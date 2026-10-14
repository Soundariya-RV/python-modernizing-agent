import yaml_is_not_installed_anywhere

def load(name):
    mod = __import__('shop.util')
    other = __import__(name)
    return mod, other
