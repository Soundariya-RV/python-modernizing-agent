import unittest

import mathx


class MathxTest(unittest.TestCase):
    def test_division(self):
        self.assertEqual(mathx.half(7), 3)
        self.assertEqual(mathx.half(-7), -4)
        self.assertEqual(mathx.mean([1, 2]), 1.5)
        r = mathx.Ratio(1, 2) / mathx.Ratio(1, 4)
        self.assertEqual((r.n, r.d), (4, 2))
        self.assertFalse(mathx.Ratio(0, 1))

    def test_dicts(self):
        self.assertEqual(mathx.describe({'x': 1, 'z': 2}), 'x=1, z=2, x-only')

    def test_types(self):
        self.assertEqual(mathx.kinds(3), 'int')
        self.assertEqual(mathx.kinds('a'), 'text')
        self.assertEqual(mathx.kinds(1.0), 'other')

    def test_ranges(self):
        self.assertEqual(mathx.evens(5), [0, 2, 4])
        self.assertEqual(mathx.squares(3), [0, 1, 4, 9])

    def test_statements(self):
        self.assertEqual(mathx.parse('5'), 5)
        self.assertEqual(mathx.parse('a'), None)
        self.assertEqual(mathx.run('result = 6 * 7'), 42)
        self.assertTrue(mathx.differ(1, 2))
        self.assertRaises(ValueError, mathx.fail)
        mathx.show(1, 2)

    def test_modules(self):
        self.assertEqual(mathx.queue_size(3), 3)
        self.assertTrue(mathx.cfg() is not None)
        self.assertEqual(mathx.buf('abc'), 'abc')
        self.assertTrue(mathx.big())
        self.assertEqual(mathx.label(3), '3!')
        self.assertEqual((mathx.MASK, mathx.PERM), (255, 493))


if __name__ == '__main__':
    unittest.main()
