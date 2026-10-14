import sys
from shop import pricing
print "report", pricing.format_cents(int(sys.argv[1]))
data = open('README.txt').read()
missing = open('does_not_exist.cfg')
if __name__ == '__main__':
    pass
