import pricing                      # implicit relative -> shop/pricing.py
from shop.util import slugify       # absolute

class Item(object):
    def __init__(self, name, cents):
        self.name, self.cents = name, cents
        self.slug = slugify(name)

    def price(self):
        return pricing.format_cents(self.cents)
