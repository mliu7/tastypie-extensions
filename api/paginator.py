from __future__ import unicode_literals

from django.core.paginator import Paginator

from endless_pagination.paginator import DefaultPaginator, CustomPage
from tastypie.paginator import Paginator as TastypiePaginator


class BasePaginator(TastypiePaginator):
    def get_limit(self):
        """ Add the limit variable to the paginator object for later use """
        self.limit = super(BasePaginator, self).get_limit()
        return self.limit

    def get_slice(self, limit, offset):
        """ Rewrite this method to return 0 items if limit is zero instead of everything.
            
            Slices the result set to the specified ``limit`` & ``offset``.
        """
        # If it's zero, return everything.
        if limit == 0:
            return []

        return self.objects[offset:offset + limit]

    def get_count(self):
        """  If the limit is 0, return the count as 0 as well """
        if self.limit == 0:
            return 0
        else:
            return super(BasePaginator, self).get_count()

class RenderedResourcePaginator(DefaultPaginator):
    def page(self, number):
        number = self.validate_number(number)
        bottom = 0 if number == 1 else ((number-2)*self.per_page + self.first_page)
        top = bottom + self.get_current_per_page(number)
        if top + self.orphans >= self.count:
            top = self.count

        # Don't actually choose specific objects from the list, just keep track of the indicies
        return CustomPage(self.object_list, number, self)

    def _get_count(self):
        return self.object_list.get('meta').get('total_count')

    count = property(_get_count)
