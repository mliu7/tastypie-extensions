from __future__ import unicode_literals

from simplejson.decoder import JSONDecodeError
import hashlib
import inspect
import re

from django.conf import settings
from django.conf.urls.defaults import *
from django.contrib.auth.models import AnonymousUser
from django.core import urlresolvers
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from django.core.urlresolvers import reverse
from django.db.models.fields import FieldDoesNotExist
from django.db.models.query import Q
from django import forms
from django.http import HttpResponse, Http404
from django.utils.html import escape as esc
from django.views.decorators.csrf import csrf_exempt

from tastypie import fields, http
from tastypie.bundle import Bundle
from tastypie.cache import NoCache
from tastypie.exceptions import ImmediateHttpResponse, BadRequest
from tastypie.http import HttpUnauthorized, HttpForbidden, HttpNotFound, HttpBadRequest
from tastypie.utils.mime import build_content_type
from tastypie.resources import Resource, ModelResource, ModelDeclarativeMetaclass

from api.fields import BaseForeignKey, BaseRelatedField
from api.forms import BaseModelResourceForm, BaseModelResourceListForm
from api.paginator import BasePaginator
from api.serializers import BaseSerializer
from api.exceptions import Http410
from api.utils import clean_html, isoformat
from oauth2app.authenticate import Authenticator
from oauth2app.models import AccessRange
from trackable_object.exceptions import Http410

num_regex = '[0-9]+'


class BaseModelDeclarativeMetaclass(ModelDeclarativeMetaclass):
    def __new__(cls, name, bases, attrs):
        """ Fix the default tastypie Resource Metaclass because it forgot to 
            initialize __bases__ which classes use to track their inheritance tree
        """
        new_class = super(BaseModelDeclarativeMetaclass, cls).__new__(cls, name, bases, attrs)
        new_class.__bases__ = bases
        return new_class


class BaseResource(Resource):
    locally_accessed = False # True if this resource is accessed from our Django module
                             # False if it was accessed normally (i.e. from an external request)

    def __init__(self, *args, **kwargs):
        super(BaseResource, self).__init__(*args, **kwargs)
        if self._meta.num_resource_ids > 1 and hasattr(self.fields, 'id'):
            # Remove the 'id' field from the resource if it uses more than one id as its identifier
            self.fields.pop('id')

    def base_urls(self):
        """ Override the standard Tastypie URLs so none of them can be called """
        return []

    def create_response(self, request, data, response_class=HttpResponse, **response_kwargs):
        """ Overrides the tastypie create_response to add the hosts to the urls that live in 
            the Meta part of the response
        """
        api_uri_keys = self._meta.api_uri_keys

        if isinstance(data, dict): # it is a dict with a list of objects
            if data.get('meta'):
                data['meta'] = self._format_api_uri(request, data['meta'], api_uri_keys)
            self.bundle.data = data
        else: # it is a bundle that represents a single object
            if data.data.get('meta'):
                data.data['meta'] = self._format_api_uri(request, data.data['meta'], api_uri_keys)
            self.bundle = data
        return super(BaseResource, self).create_response(request, data, response_class, **response_kwargs)

    def dehydrate(self, bundle):
        """ Uses the dehydrate hook to manipulate the data at the last possible moment

            Does the following things:
                - For every URL in the response, turns it into an absolute url with the domain
                    i.e. instead of /jobs/2/, it changes to 
                         https://api.domain.com/jobs/2/ or whatever server it is running on
                - For every trackable_object in the response that refers to an ID field,
                    it returns the id of the trackable object instead of the name

            This solution was recommended by the author of Tastypie himself:
                http://groups.google.com/group/django-tastypie/browse_thread/thread/123e1df9fed4176
        """
        request = self.request
        api_uri_keys = self._meta.api_uri_keys

        data = bundle.data
        data = self._format_api_uri(request, data, api_uri_keys)
        data = self._format_id_fields(data)
        bundle.data = self._escape_fields(data)
        return bundle

    def dispatch(self, request_type, request, **kwargs):
        """ A thin wrapper around the Tastypie dispatch method that saves the request variables to the object 
        
            Args:
                request_type - either 'list' or 'detail'
                request
                kwargs
        """
        self.request_type = request_type
        self.method = request.method.upper()
        self.request = request
        self.request_kwargs = kwargs.copy()
        try:
            return super(BaseResource, self).dispatch(request_type, request, **kwargs)
        except JSONDecodeError:
            # Raise a useful error message telling the user the JSON was malformed.
            self.raise_error("The data passed in is not properly formatted JSON.", HttpBadRequest)

    def do_if_authorized(self, object, action):
        """ Performs a TrackableObject action on an object if the user is authorized to do so

            Possible actions are: 'submit', 'edit', 'remove'

            Sample usage:
                self.do_if_authorized(request, object, 'submit')
        """
        if self.is_authorized(self.request, object):
            return object.__getattribute__(action)(self.request)

    def form_factory(self, type, **kwargs):
        """ Creates a form class for the resource that can be used in other places like views

            It implements the save method so users of the form get full access to all of the 
            form's error checking, as well as it's saving implementation

            Args:
                type - Options: 'list', 'create', 'update'
                kwargs - all kwargs will be passed into the form constructor
                         an example of something useful to pass in is 'instance'
        """
        assert type == 'create' or type == 'update' or type == 'list' or type == 'get'

        extra_kwargs = {}
        # Add any additional kwargs to this that should be passed to the form class 
        # Any key/value pairs on bundle.data will be passed into the form constructor when
        # the validator runs is_valid

        if type == 'get':
            if hasattr(self._meta, 'get_validation_form'):
                BaseFormClass = self._meta.get_validation_form
            else:
                raise AttributeError(("The Resource you are trying to create a form for does not "
                                      "have a form class specified in meta. The meta attribute "
                                      "'get_validation_form' needs to be specified."))
        elif type == 'create':
            if hasattr(self._meta, 'create_validation_form'):
                BaseFormClass = self._meta.create_validation_form
            else:
                raise AttributeError(("The Resource you are trying to create a form for does not "
                                      "have a form class specified in meta. The meta attribute "
                                      "'create_validation_form' needs to be specified."))
        elif type == 'list':
            if hasattr(self._meta, 'list_validation_form'):
                BaseFormClass = self._meta.list_validation_form
            else:
                raise AttributeError(("The Resource you are trying to create a form for does not "
                                      "have a form class specified in meta. The meta attribute "
                                      "'list_validation_form' needs to be specified."))
        elif type == 'update':
            if hasattr(self._meta, 'update_validation_form'):
                BaseFormClass = self._meta.update_validation_form
            else:
                raise AttributeError(("The Resource you are trying to create a form for does not "
                                      "have a form class specified in meta. The meta attribute "
                                      "'update_validation_form' needs to be specified."))
        else:
            raise NotImplementedError

        resource = self
        class FormClass(BaseFormClass):
            def __init__(self, *args, **kwargs):
                # Add this resource's class as an attribute on the form so the form can 
                # access the attribute's methods when it needs to
                self.resource = resource
                self.type = type
                kwargs.update(extra_kwargs)
                return super(FormClass, self).__init__(*args, **kwargs)

        return FormClass

    def full_dehydrate(self, bundle):
        """ Add the request object to the bundle so dehydrating related resources works ok """
        bundle.request = self.request
        return super(BaseResource, self).full_dehydrate(bundle)

    def get_acceptable_scopes(self, request):
        """ Based on the request, return a list of OAuth2app AccessRange objects that are acceptable for this request. """
        return [AccessRange.objects.get(key='universal')]

    def get_identifier(self, request):
        return request.user

    def get_resource_list_uri(self):
        return reverse('api_dispatch_list', kwargs={'resource_name': self._meta.resource_name}, urlconf='api.urls')

    def get_resource_uri(self, bundle):
        return reverse('api_dispatch_detail', kwargs={'resource_name': self._meta.resource_name,
                                                      'resource_id': bundle.obj.id}, urlconf='api.urls')

    def is_authenticated(self, request, **kwargs):
        if request.user and request.user.is_authenticated():
            # If the user is already logged in (i.e. if this is being accessed through views rather than HTTP)
            pass
        else:
            scopes = self.get_acceptable_scopes(request)
            authenticator = Authenticator(scope=scopes)
            try:
                authenticator.validate(request)
                request.user = authenticator.user # Set the user to the owner of the access_token
            except Exception, e:
                if self.method == "GET":
                    request.user = AnonymousUser()
                else:
                    self.raise_error(e.args[0], HttpUnauthorized)
        return True

    def is_authorized(self, request, object=None):
        """ Checks that the user making the request has the privileges to do what they have asked to do

            Assumes that the user object has already been added to the request variable
        """
        if not object:
            return True
        else:
            request_method = request.META.get('REQUEST_METHOD')
            if request_method == "GET":
                return True
            elif request_method == "POST":
                return request.user.is_authenticated()
            elif request_method == "PUT":
                return request.user.is_authenticated() # TODO: Create permissions on individual objects
            elif request_method == "DELETE":
                return request.user.is_authenticated() # TODO: Create permissions on individual objects
            self.method = request_method
        return False

    def override_urls(self):
        list_url = url(r"^(?P<resource_name>{0})/$".format(self._meta.resource_name), self.wrap_view('dispatch_list'), name='api_dispatch_list')
        urls = [list_url]

        if self._meta.num_resource_ids == 1:
            detail_url = url(r"^(?P<resource_name>{0})/(?P<resource_id>{1})/$".format(self._meta.resource_name, num_regex), self.wrap_view('dispatch_detail'), name='api_dispatch_detail')
            urls.append(detail_url)

        elif self._meta.num_resource_ids == 2:
            detail_url = url(r"^(?P<resource_name>{0})/(?P<resource_id_1>{1})/(?P<resource_id_2>{1})/$".format(self._meta.resource_name, num_regex), self.wrap_view('dispatch_detail'), name='api_dispatch_detail')

            urls.append(detail_url)

        return urls

    def raise_error(self, response_message, response_class):
        """ Use this method to raise errors immediately.

            Args:
                response_message - the message to send to the user describing the error
                response_class - the class that this error belongs to that then generates the error code
        """
        raise ImmediateHttpResponse(
            self.create_response(self.request, {'error_message': response_message}, response_class)
        )

    # Private methods
    def _format_uri(self, request, object_data, keys, base_url):
        """ Does a majority of the work in implementing _format_api_uri
        
            Args:
                request
                object_data - The data dictionary for the object you are create the URI for
                keys - A list of the attributes you want to turn into URIs
                base_url - The base URL you want to use for constructing this URI.
        """
        for key in keys:
            if object_data.get(key):
                object_data[key] = "{0}/{1}".format(base_url, object_data[key].lstrip('/'))
        return object_data

    def _format_api_uri(self, request, object_data, keys):
        # Add any uris to the list of keys by finding ones that end with _uri
        keys += [x for x in object_data if x.endswith('_uri') and x not in keys]
        return self._format_uri(request, object_data, keys, settings.BASE_API_URL)

    def _format_id_fields(self, object_data):
        """ Adds an id field to a related object in object_data and then returns the updated object

            args - 
                ojbect_data - a dictionary of data that needs to be formatted.
        """
        for key in object_data.keys():
            if isinstance(object_data[key], Bundle):
                id_field_name = "{0}_id".format(key)
                object_data[id_field_name] = object_data[key].data.get('id', None)
            elif isinstance(self.fields.get(key), BaseForeignKey) or \
                 isinstance(self.fields.get(key), BaseRelatedField):
                # If this field is a pointer to another resource but is None,
                # still add None to the id field so the attribute exists
                id_field_name = "{0}_id".format(key)
                object_data[id_field_name] = None

        return object_data

    def _escape_fields(self, object_data, html_fields = ['info']):
        """ Escapes all unicode or string fields in a dictionary 
        
            Args:
                object_data - A dict of the data that is going to be returned to the user
                html_fields - a list of field names where HTML is ok
        """
        for key in object_data.keys():
            if key in self._meta.dont_escape:
                continue
            if key in html_fields:
                object_data[key] = clean_html(object_data[key])
            elif isinstance(object_data[key], str) or isinstance(object_data[key], unicode):
                object_data[key] = clean_html(object_data[key], acceptable_elements=[])
        return object_data

    class Meta:
        fields = ['id'] # Disable all model fields so we can add/manipulate them manually
        always_return_data = True
        default_format = 'application/json'
        detail_allowed_methods = ['get', 'put', 'delete']
        include_absolute_url = False # We must inherit from ModelResource for this to work properly
        include_resource_uri = True
        paginator_class = BasePaginator
        limit = 100
        max_limit = 200
        serializer = BaseSerializer(formats=['json'])
        list_allowed_methods = ['get', 'post']
        api_uri_keys = ['resource_uri', 'next', 'previous']
        dont_escape = [] # A list of fields that should not be run through the resource's escape method
        num_resource_ids = 1 # The number of resource ids that will be passed in as parameters.
                             # Only 1 or 2 are valid choices for this parameter
                             # If 2 is specified, obj_get and obj_


class BaseModelResource(BaseResource, ModelResource):

    __metaclass__ = BaseModelDeclarativeMetaclass

    def __init__(self, *args, **kwargs):
        # Loop over all parent classes to add any base fields that don't already
        # exist on the current resource
        parent_classes = inspect.getmro(self.__class__)
        for parent in parent_classes:
            if hasattr(parent, 'base_fields'):
                base_fields = parent.base_fields
                for key in base_fields:
                    if not key in self.base_fields:
                        self.base_fields[key] = base_fields[key]

        super(BaseModelResource, self).__init__(*args, **kwargs)

    def alter_queryset(self, queryset, filters=None):
        """ Gets called after the filters are built but before a list of resources is looked up.

            This hook, if implemented, allows you to change the queryset defined on Meta
        """
        return queryset

    def apply_sorting(self, obj_list, options=None):
        """ Sorts the queryset based on the order by string listed in the parameter 'order_by'

            For each ordering string specified, if the attribute does not exist on the django model,
            this method checks for a dictionary named 'maps_to' on the Meta class of the resource. 
            The keys for 'maps_to' correspond to resource attributes and the corresponding values 
            are the model attributes. 
        """
        # Replace options with the cleaned data from the bundle for this resource
        options = self.bundle.data

        if not 'order_by' in options:
            return obj_list

        else:
            final_ordering_list = []
            select = {}
            ordering_string_list = options.get('order_by')

            # Get the meta class for the queryset
            meta = obj_list.model._meta

            if ordering_string_list:
                # For each string in the list, clean it up, and do some checks before adding it to the real ordering list
                for ordering_string in ordering_string_list:
                    # Remove the preceding '-' if it exists
                    positive_ordering_string = ordering_string.lstrip('\'\"').rstrip('\'\"')
                    prefix = ''
                    if positive_ordering_string.startswith('-'):
                        positive_ordering_string = positive_ordering_string[1:]
                        prefix = '-'

                    # Clean the ordering string if it ends in _id
                    suffix = ''
                    if positive_ordering_string.endswith('_id'):
                        positive_ordering_string = positive_ordering_string[:-3]
                        suffix = '_id'

                    # Check that the attribute is valid
                    attribute = getattr(self, positive_ordering_string, None)
                    if not attribute or \
                       suffix == '_id' and not (isinstance(attribute, BaseForeignKey) or \
                                                isinstance(attribute, BaseRelatedField)):
                        # Throw an error because they are trying to order on a string that is
                        # not an attribute on this resource
                        response_message = "The attribute '{0}' does not exist on this resource.".format(ordering_string)
                        self.raise_error(response_message, http.HttpBadRequest)

                    # Get the dictionary of attribute mappings for this resource
                    maps_to = self.Meta.maps_to
                    
                    # If it is specified in 'maps_to' on the Meta class of the resource
                    if positive_ordering_string in maps_to:

                        # If it is a simple mapping (i.e. no arithmetic or anything)
                        if re.match('[a-z_0-9]+$', maps_to[positive_ordering_string]):
                            final_ordering_list.append(prefix + maps_to[positive_ordering_string])

                        # If it is a complex mapping
                        else:
                            # Use aggregate to add this attribute to the queryset
                            select[positive_ordering_string + '_for_api_ordering'] = maps_to[positive_ordering_string]

                            # Add this ordering to the list of orderings
                            final_ordering_list.append(prefix + positive_ordering_string + '_for_api_ordering')

                    else: # It's not in maps to, but still may be an attribute on the django model

                        try:
                            # If it is an attribute on the django model for this resource
                            meta.get_field_by_name(positive_ordering_string)

                            # If this does not throw an exception, add this ordering to the list of orderings
                            final_ordering_list.append(prefix + positive_ordering_string)

                        except FieldDoesNotExist:
                            # Return a useful error message saying we cannot order on this attribute
                            response_message = "Cannot order on '{0}'.".format(ordering_string)
                            self.raise_error(response_message, http.HttpBadRequest)
            else:
                final_ordering_list = ['-id'] #Order by most recent to make the ordering non-ambiguous

        if select:
            obj_list = obj_list.extra(select=select)

        return obj_list.order_by(*final_ordering_list)

    def build_complex_filters(self, filters=None):
        """ Returns a Q object of any complex filters for the query """
        return Q()

    def dehydrate_id(self, bundle):
        return bundle.obj.id

    def dehydrate_time_created(self, bundle):
        """ The time the object was created in ISO format """
        return isoformat(bundle.obj.submitted_time)

    def dehydrate_time_last_updated(self, bundle):
        """ The time the object was most recently updated in ISO format """
        if bundle.obj.action_time:
            return isoformat(bundle.obj.action_time)
        return isoformat(bundle.obj.submitted_time)

    def filter_fields(self, fields):
        """ Takes in a list of fields and removes all fields on the resource except for the fields
            specified in the list. If fields is None or empty, this does nothing
        """
        self.ignore_fields = []
        if fields:
            # Specify any fields that must show up no matter what 
            permanent_fields = ['resource_uri']

            # Create the valid id field names
            id_fields = []
            for declared_field_name, declared_field_object in self.declared_fields.items() + self.fields.items():
                if isinstance(declared_field_object, BaseForeignKey):
                    id_fields.append("{0}_id".format(declared_field_name))

            # Specify fields defined on the resource
            initial_fields = self.fields.keys() + self.declared_fields.keys() + self.Meta.fields + id_fields

            # Make a list of all valid fields
            valid_fields = initial_fields + permanent_fields

            # Check that all the fields live on the resource
            for field in fields:
                if field not in valid_fields:
                    response_message = "Field '{0}' is not a valid field.".format(field)
                    response_class = http.HttpBadRequest
                    self.raise_error(response_message, response_class)

            # Remove the fields from the resource
            for field in initial_fields:
                if field not in fields and \
                   field not in permanent_fields:
                    self.ignore_fields.append(field)

    def full_dehydrate(self, bundle):
        """
        Given a bundle with an object instance, extract the information from it
        to populate the resource.
        """
        # Dehydrate each field.
        for field_name, field_object in self.fields.items():
            if hasattr(self, 'ignore_fields') and field_name in self.ignore_fields:

                # If it's a foreign key that's being ignored 
                if isinstance(field_object, BaseForeignKey):

                    # If the foreign key for the object is not also ignored, add the _id field
                    id_field = "{0}_id".format(field_name)
                    if id_field in self.ignore_fields:
                        continue
                    else:
                        field_name = id_field
                        field_object = fields.IntegerField(id_field, null=True)
                else:
                    continue

            # A touch leaky but it makes URI resolution work.
            if getattr(field_object, 'dehydrated_type', None) == 'related':
                field_object.api_name = self._meta.api_name
                field_object.resource_name = self._meta.resource_name

            bundle.data[field_name] = field_object.dehydrate(bundle)

            # Check for an optional method to do further dehydration.
            method = getattr(self, "dehydrate_%s" % field_name, None)

            if method:
                bundle.data[field_name] = method(bundle)

        bundle = self.dehydrate(bundle)
        return bundle

    def cached_full_dehydrate(self, bundle, **kwargs):
        """ Eliminate caching for now """
        return self.full_dehydrate(bundle)

    def get_detail(self, request, **kwargs):
        """
        Returns a single serialized resource.

        Calls ``cached_obj_get/obj_get`` to provide the data, then handles that result
        set and serializes it.

        Should return a HttpResponse (200 OK).

        Users should implement 'generate_obj_detail_cache_key' to construct the cache key for 
        a resource based on the object the resource is referring to.
        """
        self.bundle = Bundle() # Create an empty bundle and save it here for consistency across views
        self.bundle.data = request.GET.copy() 
        self.is_valid(bundle=self.bundle, request=request)

        # If fields was passed in as an argument, remove all fields except for these
        fields = self.bundle.data.get('fields', None)
        self.filter_fields(fields)
        
        try:
            obj = self.obj_get(request=request, **self.remove_api_resource_names(kwargs))
        except ObjectDoesNotExist:
            return http.HttpNotFound()
        except MultipleObjectsReturned:
            return http.HttpMultipleChoices("More than one resource is found at this URI.")

        bundle = self.build_bundle(obj=obj, request=request)
        bundle = self.cached_full_dehydrate(bundle, **kwargs)
        bundle = self.alter_detail_data_to_serialize(request, bundle)
        return self.create_response(request, bundle)

    def get_list(self, request, **kwargs):
        """
        Returns a serialized list of resources.

        Calls ``obj_get_list`` to provide the data, then handles that result
        set and serializes it.

        Should return a HttpResponse (200 OK).
        """
        # TODO: Uncached for now. Invalidation that works for everyone may be
        #       impossible.
        self.bundle = Bundle(data=request.GET.copy())
        self.bundle.queryset = None
        self.is_valid(bundle=self.bundle, request=request)

        # If fields was passed in as an argument, remove all fields except for these
        fields = self.bundle.data.get('fields', None)
        self.filter_fields(fields)

        objects = self.obj_get_list(request=request, **self.remove_api_resource_names(kwargs))
        sorted_objects = self.apply_sorting(objects, options=request.GET)

        paginator = self._meta.paginator_class(request.GET, sorted_objects, resource_uri=self.get_resource_list_uri(), limit=self._meta.limit)
        to_be_serialized = paginator.page()

        # Dehydrate the bundles in preparation for serialization.
        bundles = [self.build_bundle(obj=obj, request=request) for obj in to_be_serialized['objects']]
        to_be_serialized['objects'] = [self.cached_full_dehydrate(bundle, **kwargs) for bundle in bundles]
        to_be_serialized = self.alter_list_data_to_serialize(request, to_be_serialized)
        return self.create_response(request, to_be_serialized)

    def is_valid(self, bundle, request):
        """ Handles checking if the data provided by the user is valid.

            If validation fails, an error is raised with the error messages
            serialized inside it.

            Args:
                bundle - bundle that already has the user entered data on it.
                request 
        """
        method = request.META['REQUEST_METHOD']
        instance = None

        # Add the resource id and object to the bundle so the validators can use it if necessary
        if self.request_type == 'detail' and (method == 'PUT' or method == 'DELETE'):
            resource_ids = self._get_resource_ids()
            if len(resource_ids) == 1:
                bundle.data['id'] = resource_ids[0]
            bundle.obj = self._lookup_obj()
            instance = bundle.obj

        validation_form = self._meta.validation
        extra_kwargs = {'request': request}

        # If this resource was accessed by our own Django code, lift the max limit restrictions
        if self.locally_accessed:
            extra_kwargs['ignore_limit'] = True

        # Choose the correct validation form
        if self.request_type == 'detail' and method == 'GET' and hasattr(self._meta, 'get_validation_form'):
            form_class = self.form_factory('get')
        elif self.request_type == 'list' and method == 'GET' and hasattr(self._meta, 'list_validation_form'):
            form_class = self.form_factory('list')
        elif self.request_type == 'detail' and method == 'PUT' and hasattr(self._meta, 'update_validation_form'):
            form_class = self.form_factory('update')
        elif self.request_type == 'list' and  method == 'POST' and hasattr(self._meta, 'create_validation_form'):
            form_class = self.form_factory('create')
        else:
            form_class = None

        errors = {}

        if form_class:
            data = bundle.data
            form = form_class(data, **extra_kwargs)
            form.instance = instance
            if form.is_valid():
                bundle.data = form.cleaned_data
            else:
                errors.update(form.errors)

        if len(errors):
            # Put all of the errors under the 'errors' label in the response
            errors = {'errors': errors}

            if request:
                desired_format = self.determine_format(request)
            else:
                desired_format = self._meta.default_format

            serialized = self.serialize(request, errors, desired_format)
            response = http.HttpBadRequest(content=serialized, content_type=build_content_type(desired_format))
            raise ImmediateHttpResponse(response=response)

    def obj_create(self, bundle, **kwargs):
        bundle = self.full_hydrate(bundle)
        bundle.obj.id = None
        if bundle.obj.is_live():
            bundle.obj = self.do_if_authorized(bundle.obj, 'submit')
        elif bundle.obj.is_hidden():
            bundle.obj = self.do_if_authorized(bundle.obj, 'submit_hidden')
        self.bundle = bundle
        return bundle

    def obj_get(self, request, **kwargs):
        """ Fetches a single object. 
        
            This is only called on HTTP GET requests at a resource detail endpoint 
        """
        obj = self._lookup_obj()
        self.bundle.obj = obj # Save the obj on the bundle for consistency
        return obj

    def obj_get_list(self, request, **kwargs):
        """ Fetches a list of objects. 
        
            This only gets called on an HTTP GET request to a resource list endpoint
        """
        # Make a copy of all the user-entered filters
        filters = self.bundle.data.copy()

        # Adjust the base queryset
        queryset = self.alter_queryset(queryset=self.Meta.queryset, filters=filters)

        # Create the args and kwargs that will be used as filters
        applicable_filters = self.build_filters(filters=filters)
        complex_filters = self.build_complex_filters(filters=filters)
        select_related = self.Meta.select_related

        try:
            # Apply the filters
            base_object_list = queryset.filter(complex_filters, **applicable_filters).filter_view_perms(request.user).select_related(*select_related)

            # Save the queryset
            self.bundle.queryset = base_object_list

            # Check the rate limiting
            return self.apply_authorization_limits(request, base_object_list)
        except ValueError:
            self.raise_error("Invalid resource lookup data provided.", http.HttpBadRequest)

    def obj_update(self, bundle, request, **kwargs):
        original_status = bundle.obj.status
        bundle = self.full_hydrate(bundle)

        bundle.obj = self.do_if_authorized(bundle.obj, 'edit')
        if not bundle.obj:
            self.bundle = bundle
            self.raise_error("You are not authorized to edit this object", HttpUnauthorized)
        self.bundle = bundle
        return bundle

    def obj_delete(self, **kwargs):
        obj = self._lookup_obj()
        return self.do_if_authorized(obj, 'remove')

    def override_urls(self):
        urls = super(BaseModelResource, self).override_urls()

        # Add the merge URL
        if self._meta.num_resource_ids == 1:
            merge_url = url(r"^(?P<resource_name>{0})/merge/(?P<resource_id_1>{1})/(?P<resource_id_2>{1})/$".format(self._meta.resource_name, num_regex), self.wrap_view('merge'), name='api_merge')
            unmerge_url = url(r"^(?P<resource_name>{0})/unmerge/(?P<resource_id>{1})/$".format(self._meta.resource_name, num_regex), self.wrap_view('unmerge'), name='api_unmerge')

            urls.append(merge_url)
            urls.append(unmerge_url)
        return urls

    def partial_dehydrate(self, bundle):
        bundle.data['id'] = self.dehydrate_id(bundle)
        bundle.data['resource_uri'] = self.dehydrate_resource_uri(bundle)
        if hasattr(self, 'dehydrate_name'):
            bundle.data['name'] = self.dehydrate_name(bundle)
        elif hasattr(self, 'name') and hasattr(bundle.obj, 'name'):
            bundle.data['name'] = bundle.obj.name
        if hasattr(self, 'short_name') and hasattr(bundle.obj, 'short_name'):
            bundle.data['short_name'] = bundle.obj.short_name
        bundle = self.dehydrate(bundle)
        return bundle

    def _get_obj_from_ids(self, ids, queryset):
        """ Gets an object from its resource ids. If no object could be found, this function
            should raise an Http404 exception.
        
            Args:
                ids - a List of integers representing object ids.
                      if only one resource_id was supplied to the URI, then the list will be of size
                      1. If 2 resource_ids were supplied to the URI, then the list will be of size 2
                queryset - the queryset that the object will be looked up from using the ids
        """
        return queryset.get_from_id(ids[0], select_related=self.Meta.select_related)

    def _get_resource_ids(self, num_resource_ids=None):
        """ Takes a user-entered dict of kwargs, and returns a list of resource_ids.
            If any of the resource_ids are invalid in any way, it throws raises an immediate error

            Args:
                num_resource_ids - The number of resource ids specified on the URL.
                                   Defaults to the number specified on the Resource's Metaclass
        """
        if not num_resource_ids:
            num_resource_ids = self._meta.num_resource_ids

        if num_resource_ids == 1:
            resource_id = self.request_kwargs.get('resource_id', -1)
            if resource_id == -1:
                self.raise_error("Invalid resource lookup data provided. Please provide a valid resource_id (positive integer) that matches the id of an existing resource.", http.HttpBadRequest)
            return [resource_id]
        elif num_resource_ids == 2:
            resource_id_1 = self.request_kwargs.get('resource_id_1', -1)
            resource_id_2 = self.request_kwargs.get('resource_id_2', -1)
            if resource_id_1 == -1 or resource_id_2 == -1:
                self.raise_error("Invalid resource lookup data provided. Please provide valid resource_ids (positive integers) that match the ids of existing resources.", http.HttpBadRequest)
            return [resource_id_1, resource_id_2]
        return []

    def _lookup_obj(self, queryset=None, resource_ids=None):
        """ Takes an id of a TrackableObject and looks it up using the queryset specified on the resource.

            Args:
                id - ID of a TrackableObject object to be looked up
                queryset - (optional) the queryset to use for the lookup. 
                                      defaults to the queryset specified in the resource's Meta class

            Additional notes:
                Only live objects or objects that are hidden but the user owns are returned.
                All other objects will throw an error.
        """
        if not resource_ids:
            resource_ids = self._get_resource_ids()
        if not queryset:
            queryset = self.Meta.queryset
        try:
            object = self._get_obj_from_ids(resource_ids, queryset)
            if not object:
                raise Http404
            elif object.has_view_perm(self.request.user): 
                return object
            elif object.is_hidden():
                response_message = "You are not authorized to access this resource \
                                    because the owner has marked it as hidden."
                response_class = http.HttpForbidden
            elif object.is_removed():
                response_message = "This resource has been removed and is no longer accessible."
                response_class = http.HttpGone
            else:
                response_message = "You are not authorized to access this resource."
                response_class = http.HttpForbidden
        except Http404, e:
            response_message = "A resource with this id could not be found."
            response_class = http.HttpNotFound
        except Http410, e:
            response_message = "This resource has already been removed and is no longer accessible."
            response_class = http.HttpGone

        self.bundle = Bundle() 
        self.raise_error(response_message, response_class)

    class Meta(BaseResource.Meta):
        select_related = []
        get_validation_form = BaseModelResourceForm
        maps_to = {}
        cache = NoCache()
