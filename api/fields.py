from __future__ import unicode_literals

from datetime import time as dttime
import time
import dateutil
import pytz
import re

from django import forms

from tastypie import fields

from timezones.zones import AMERICA_FIRST_TIMEZONE_CHOICES
from trackable_object.models import TrackableObject


def _dehydrate_related(bundle, related_resource, full=False):
    """
    Extends the default tastypie dehydrate_related to use our partial_dehydrate method

    Based on the ``full_resource``, returns either the endpoint or the data
    from ``full_dehydrate`` for the related resource.
    """
    # Check to make sure we need to do anything
    if not related_resource.instance:
        return None

    # Give the related_resource a request object to use for the dehydrate cycle
    related_resource.request = bundle.request

    bundle = related_resource.build_bundle(obj=related_resource.instance, request=bundle.request)

    if not full:
        # Add the id, resource_uri and name for the resource
        return related_resource.partial_dehydrate(bundle)
    else:
        # ZOMG extra data and big payloads.
        return related_resource.full_dehydrate(bundle)


class BaseForeignKey(fields.ForeignKey):
    """ Tastypie Field for object foreign keys """
    def dehydrate_related(self, bundle, related_resource):
        return _dehydrate_related(bundle, related_resource, self.full)

    def resource_from_pk(self, fk_resource, obj, request=None, related_obj=None, related_name=None):
        fk_resource.request = request
        return super(BaseForeignKey, self).resource_from_pk(fk_resource, obj, request=request, related_obj=related_obj, related_name=related_name)


class BaseRelatedField(fields.RelatedField):
    """ Custom field for adding related resources that aren't based solely on a Django foreignkey """

    def __init__(self, to, *args, **kwargs):
        return super(BaseRelatedField, self).__init__(to, None, *args, **kwargs)

    def dehydrate_related(self, bundle, related_resource):
        return _dehydrate_related(bundle, related_resource, self.full)

    def dehydrate(self, bundle):
        # Get the this field's name as specified in the resource
        field_name = self.instance_name

        # Construct the method name of the method that returns the related resource
        #   It will return something like "get_related_organization"
        method_name = "get_related_{0}".format(field_name)

        # Get the actual method from the resource
        resource = self._resource()
        method = resource.__getattribute__(method_name)

        # Get the related object
        instance = method(bundle)

        # Create the related resource
        related_resource = self.to()
        related_resource.instance = instance

        # Dehydrate and return the related resource
        return self.dehydrate_related(bundle, related_resource)


class ISODateTimeField(forms.RegexField):
    """ Accepts Isoformat Datetimes that include timezones 
    
        Returns a dict containing the converted datetime object in the server 
        timezone and a timezone object representing the correct timezone for the object
    """
    _invalid_error_message = ("Enter a valid ISO 8601 format time string. "
                              "Form is YYYY-MM-DDTHH:MM:SS+hh:mm")
    def __init__(self, *args, **kwargs):
        error_messages = {'invalid': self._invalid_error_message}
        super(ISODateTimeField, self).__init__('\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}',
                                               error_message=error_messages,
                                               *args, **kwargs)

    def get_timezone_string(self, datetime_with_tz, offset):
        """ Takes a datetime object that has a timezone along with an offset in seconds and 
            returns a timezone string such as "US/Central" or "US/Pacific"

            Args:
                datetime_with_tz - a Datetime object. 
                offset - The number of seconds the offset is. This can be either positive or negative
        """
        datetime_without_tz = datetime_with_tz.replace(tzinfo=None)

        # For each possible timezone
        for tz_choice in AMERICA_FIRST_TIMEZONE_CHOICES:
            # Create the timezone object
            tz = pytz.timezone(tz_choice[0])

            # Add it to the datetime object
            datetime_to_compare = tz.localize(datetime_without_tz)

            # Time difference
            difference = datetime_to_compare - datetime_with_tz

            # See if the datetime object has the right offset in seconds
            if difference.seconds == 0 and difference.days == 0:
                return tz_choice[0]

        return None

    def clean(self, value, *args, **kwargs):
        try:
            if value:
                datetime_with_tz = dateutil.parser.parse(value)

                if not datetime_with_tz.tzinfo or (datetime_with_tz.tzinfo == dateutil.tz.tzlocal()): # This happens when it finds a +00:00 timezone offset
                    # Set it to UTC
                    datetime_with_tz = datetime_with_tz.replace(tzinfo = dateutil.tz.tzutc())
                    offset_in_seconds = 0
                else:
                    offset_in_seconds = datetime_with_tz.tzinfo._offset.seconds + datetime_with_tz.tzinfo._offset.days * 24 * 60 * 60 

                timezone_string = self.get_timezone_string(datetime_with_tz, offset_in_seconds)

                if not timezone_string:
                    raise forms.ValidationError(("The timezone offset was malformed. Examples of proper timezone strings "
                                                 "are +01:30 or -06:00"))

                # Convert the time to the server timezone
                datetime_in_utc = datetime_with_tz.astimezone(pytz.utc)

                # Remove the timezone field 
                native_datetime_in_utc = datetime_in_utc.replace(tzinfo = None)

                return {'timezone': timezone_string,
                        'utc_time': native_datetime_in_utc}
            else:
                if self.required:
                    raise forms.ValidationError("This field is required")
                else:
                    return None

        except ValueError, e:
            error_message = "{0}. {1}".format(e.args[0], self._invalid_error_message)
            raise forms.ValidationError(error_message)


class TimeField(forms.RegexField):
    """ Accepts a Time in the format of hh:mm """
    _invalid_error_message = ("Enter a valid time string. "
                              "Form is hh:mm")
    def __init__(self, *args, **kwargs):
        error_messages = {'invalid': self._invalid_error_message}
        super(TimeField, self).__init__('\d{2}:\d{2}',
                                        error_message=error_messages,
                                        *args, **kwargs)

    def clean(self, value, *args, **kwargs):
        time_struct = time.strptime(value, "%H:%M")
        return dttime(hour=time_struct.tm_hour, minute=time_struct.tm_min)


class ListField(forms.RegexField):
    """ Accepts a list of arguments where each argument must match a regex specified by regex_string

        Usage. On a model specify it in the following way:

            class MyForm(forms.Form):
                my_field = ListField(max_length=5000, required=False, regex_string='\d+')
    """
    def __init__(self, regex_string='', error_message='', *args, **kwargs):
        """ regex_string is a regex specifying a regex to match a single valid list element """

        regex = '^\[(\s*[\'"]?{0}[\'"]?\s*(,\s*[\'"]?{0}[\'"]?\s*)*)?\]$'.format(regex_string)
        super(ListField, self).__init__(regex, error_message=error_message, *args, **kwargs)

    def _string_to_list(self, str):
        if str:
            splitter = re.compile(r',')
            strings = splitter.split(str.lstrip('[').rstrip(']'))
            if strings[0]:
                return [x.strip() for x in strings]
            else:
                return []
        return None

    def clean(self, value, *args, **kwargs):
        cleaned_value = super(ListField, self).clean(value, *args, **kwargs)
        cleaned_list = self._string_to_list(cleaned_value)
        if cleaned_list is None:
            return None
        return cleaned_list


class IntegerListField(ListField):
    """ Accepts Integers separated by commas. When the field runs clean, 
        it then returns a python list of integers if the input was clean. 
        Otherwise it returns an error as expected.

        Additional optional kwargs:
            max_items - (optional) maximum number of integers that can be supplied to the list
    """
    def __init__(self, *args, **kwargs):
        error_messages = {'invalid': ("Enter only a list of integers separated by "
                                      "commas. Examples of valid inputs are: [], "
                                      "[1,2,3], [ 23 , 53 ]")}
        self.max_items = kwargs.pop('max_items', None)
        super(IntegerListField, self).__init__(regex_string='\d+',
                                               error_message=error_messages, 
                                               *args, **kwargs)

    def clean(self, value, *args, **kwargs):
        cleaned_list = super(IntegerListField, self).clean(value, *args, **kwargs)

        if cleaned_list is None:
            return None

        if self.max_items and self.max_items < len(cleaned_list):
            raise forms.ValidationError(("Too many elements supplied to the list. The max number for this "
                                         "list is {0}").format(self.max_items))
        return [int(x) for x in cleaned_list]
