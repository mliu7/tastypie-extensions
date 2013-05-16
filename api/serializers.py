from __future__ import unicode_literals

from tastypie.serializers import Serializer


class BaseSerializer(Serializer):
    def to_html(self, data, options=None):
        """ Overrides Serializer's implementation to return JSON by default """
        return self.to_json(data, options)
