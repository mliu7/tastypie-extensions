from __future__ import unicode_literals

from django import forms

from tastypie.bundle import Bundle

from django.conf import settings
from api.fields import ListField


regex = '[a-z_0-9-]+'

class BaseForm(forms.Form):
    """ Extends Django's form class to add other useful things like a default value """
    def __init__(self, *args, **kwargs):
        if 'request' in kwargs:
            self.request = kwargs.pop('request')
        if 'instance' in kwargs:
            self.instance = kwargs.pop('instance')
        self.ignore_limit = kwargs.pop('ignore_limit', False)
        super(BaseForm, self).__init__(*args, **kwargs)

    # Public methods
    def save(self):
        """ Implements the save method so this form can be used in regular django views.

            It does the same validation that it usually does for the API, but instead of
            creating a JSON response, it just creates the object and then returns it.
        """
        assert hasattr(self, 'request')
        assert self.type == 'create' or self.type == 'update'

        # Use the form's cleaned_data to create a bundle
        bundle = Bundle()
        bundle.data = self.cleaned_data
        if hasattr(self, 'request'):
            bundle.request = self.request
        if hasattr(self, 'instance'):
            bundle.obj = self.instance

        # Use the resource's methods to save the bundle
        self.resource.request = self.request
        if self.type == 'create':
            bundle = self.resource.obj_create(bundle)
        elif self.type == 'update':
            assert self.request != None
            assert bundle.obj != None
            bundle = self.resource.obj_update(bundle, self.request)

        # Return the object
        return bundle.obj

    # Private methods
    def _post_clean(self):
        """ Adds default values to fields that have default values specified """
        # Get the dict of defaults specified in Meta
        defaults = self.Meta.defaults

        # Set any fields to their default values if no values were entered
        for default_key in defaults:
            if default_key in self.cleaned_data and not self.cleaned_data[default_key]:
                self.cleaned_data[default_key] = defaults[default_key]
        return self.cleaned_data

    class Meta():
        defaults = {}


class BaseModelResourceForm(BaseForm):
    fields = ListField(max_length=5000, required=False, regex_string=regex)


class BaseModelResourceListForm(BaseModelResourceForm):
    order_by = ListField(max_length=5000, required=False, regex_string=regex)

    def __init__(self, *args, **kwargs):
        visibilities = kwargs.pop('acceptable_visibilities', None)
        super(BaseModelResourceListForm, self).__init__(*args, **kwargs)

    def clean(self):
        try:
            limit = int(self.data.get('limit'))
        except:
            return self.cleaned_data

        if limit > settings.GET_LIMIT_MAX and not self.ignore_limit:
            raise forms.ValidationError(("The requested limit is above the maximum of " + str(settings.GET_LIMIT_MAX) + "."))
        return self.cleaned_data
