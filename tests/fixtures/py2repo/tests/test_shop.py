import unittest
from shop.models import Item

class ItemTest(unittest.TestCase):
    def test_price(self):
        self.assertEqual(Item('A b', 1234).price(), '12.34')

if __name__ == '__main__':
    unittest.main()
