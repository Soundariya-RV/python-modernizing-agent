from . import models                # explicit relative; models imports pricing -> cycle

def format_cents(c):
    return '%d.%02d' % (c / 100, c % 100)

def cheapest(items):
    return min(items, key=lambda i: i.cents) if items else None
