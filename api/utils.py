from __future__ import unicode_literals

import inspect
import simplejson
import sys
import pytz

from BeautifulSoup import BeautifulSoup

from api.resources.generic import BaseModelResource
from trackable_object.models import TrackableObject
from trackable_object.utils import fake_request


def get_resources():
    """ Returns a list of all resources """
    from resources import generic, jobs_resources, locations_resources, notifications_resources, payments_resources, user_resources
    resource_modules = ['generic', 'jobs_resources', 'locations_resources', 'notifications_resources', 'payments_resources', 'user_resources']
    classes = []
    for resource_module in resource_modules:
        full_module_path = 'api.resources.{0}'.format(resource_module)
        classes += inspect.getmembers(sys.modules[full_module_path], lambda x: inspect.isclass(x) and issubclass(x, BaseModelResource) and not issubclass(BaseModelResource, x))

    return classes


def get_resource_class(model):
    """ Takes a regular model and returns the Resource class that represents that model """
    resources = get_resources()
    resource_class_list = [x for x in resources if hasattr(x[1].Meta, 'queryset') and x[1].Meta.queryset.model == model]
    if resource_class_list:
        return resource_class_list[0][1]
    else:
        return None


def access_resource(resource_class, request, obj=None, method='GET', type='list', resource_ids=None, params=None, full=True, return_obj=False):
    """ Access one or more resources. Resources are returned as Python dicts.

        Args:
            resource_class - the class of the resource you want to access or a string of 
                             the resource's name. 

                             (alternately, if you don't know the resource class, you can pass in the model class)
            request - The request that was originally made by the user. This request does not
                      have to be the correct "type". This method simply uses information on the request
                      object such as the user, and then constructs a "fake request" with the appropriate
                      method and content types that simulates an API call.
            obj - (optional), the object that the resource represents
            method - 'GET', 'PUT', 'POST', or 'DELETE'
            type - 'list' or 'detail'
            resource_ids - a list of ids that specify the resource
            params - (optional) A Dict of args being used for the API call
            full - If True, GET detail will return the full serialized dictionary of info for the object,
                   If False, the GET detail call will return only a partially hydrated dict
            return_obj - If True, it returns the django object(s). (does not yet work with GET detail)
                         If False, it returns a dictionary equal to the JSON that the API usually returns
    """
    if not params:
        params = {}

    # Turn all lists from the parameter dict into strings
    for key, value in params.items():
        if isinstance(value, list):
            value = [str(x) for x in value]
            params[key] = "[{0}]".format(','.join(value))

    if isinstance(resource_class, basestring):
        resource_class_string = resource_class
        resource_class = None
        cls_list = get_resources()
        for name, cls in cls_list:
            if hasattr(cls.Meta, 'resource_name') and \
               resource_class_string == cls.Meta.resource_name:
                resource_class = cls 
                break
        if not resource_class:
            raise Exception("Cannot find resource class {0}".format(resource_class_string))
    elif issubclass(resource_class, TrackableObject): 
        # This is a Model class and we need to fetch the appropriate resource class
        resource_class = get_resource_class(resource_class)

    # Set the right content type. GETdoesn't use JSON, it just encodes the parameters into the URL
    if method == 'GET':
        content_type = 'application/x-www-form-urlencoded'
    else:
        content_type = 'application/json'

    resource = resource_class()
    resource.locally_accessed = True
    resource.request = fake_request(user=request.user, content_type=content_type, data=simplejson.dumps(params), method=method)

    # Set the right method to use
    resource.request.method = method.lower()

    if type == 'detail':
        if obj:
            if return_obj:
                return obj

            bundle = resource.build_bundle(obj=obj)

            if not full:
                # Add the id, resource_uri and name for the resource
                dehydrated_bundle = resource.partial_dehydrate(bundle)
            else:
                dehydrated_bundle = resource.full_dehydrate(bundle)

        else: # No object was specified
            kwargs = {}
            if resource_ids and len(resource_ids) == 1:
                kwargs['resource_id'] = resource_ids[0]
                resource.resource_id = resource_ids[0]
            elif resource_ids and len(resource_ids) == 2:
                kwargs['resource_id_1'] = resource_ids[0]
                kwargs['resource_id_2'] = resource_ids[1]
            else:
                raise Exception("Detail view accepts either 1 or 2 resource ids")

        if method == 'GET':
            if obj:
                serialized_data = resource.serialize(request, dehydrated_bundle, 'application/json')
                return simplejson.loads(serialized_data)
            else:
                response = resource.dispatch('detail', resource.request, **kwargs)
                if return_obj:
                    return resource.bundle.obj
                return simplejson.loads(response._container[0])

        elif method == 'DELETE':
            if obj:
                raise NotImplementedError
            else:
                response = resource.dispatch('detail', resource.request, **kwargs)
                return simplejson.loads(response._container[0])

        else:
            raise NotImplementedError
    elif type == 'list':
        if resource_ids:
            raise Exception("List type requests do not accept any resource ids resource ids")

        if method == 'GET' or method == 'POST':
            response = resource.dispatch('list', resource.request)

            if return_obj and method == 'GET':
                return resource.bundle.queryset
            elif return_obj and method == 'POST':
                return resource.bundle.obj
            else:
                return simplejson.loads(response._container[0])

        else:
            raise NotImplementedError


def isoformat(dt):
    """ Takes a datetime stored returns the time in an isoformat, accounting
        for daylight savings time. 

        Args:
            dt - Python Datetime object in any timezone. If it does not contain a tzinfo object (i.e.
                 if it is a native datetime object) it is assumed to be UTC.
                 If dt is None, isoformat returns None
    """
    if not dt:
        return None
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=pytz.utc)
    return dt.isoformat()


def clean_html(fragment, acceptable_elements=['p', 'h2', 'h3', 'h4', 'b', 'strong', 'i', 'u', 'ul', 'ol', 
                                              'span', 'li', 'a', 'em'], 
               acceptable_attributes=['alt','width','href','height','title','value','style']):
    """ This method takes in an HTML fragment and makes it safe by removing any unacceptable tags.
        The original solution was found here:
            http://stackoverflow.com/questions/699468/python-html-sanitizer-scrubber-filter/812785
        We used the answer by Jochen Ritzel
    """
    while True:
        soup = BeautifulSoup( fragment )
        removed = False        
        for tag in soup.findAll(True): # find all tags
            if tag.name not in acceptable_elements:
                tag.extract() # remove the bad ones
                removed = True
            else: # it might have bad attributes
                # a better way to get all attributes?
                for attr in tag._getAttrMap().keys():
                    if attr not in acceptable_attributes:
                        del tag[attr]
        # turn it back to html
        fragment = unicode(soup)
        if removed:
            # we removed tags and tricky can could exploit that!
            # we need to reparse the html until it stops changing
            continue # next round
        return fragment
