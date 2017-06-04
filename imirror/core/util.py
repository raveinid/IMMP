import importlib


def resolve_import(path):
    """
    Take a dotted name (e.g. ``<module path>.<class name>``), and return the physical class.
    """
    module, class_ = path.rsplit(".", 1)
    return getattr(importlib.import_module(module), class_)


class Base(object):
    """
    Utility class to provide a default :meth:`__repr__` based on the contents of :attr:`__dict__`.
    """

    def __repr__(self):
        def nest_repr(obj):
            if isinstance(obj, dict):
                return "{...}"
            elif isinstance(obj, list):
                return "[...]"
            else:
                return repr(obj)
        items = ("{}={}".format(k, nest_repr(v)) for k, v in self.__dict__.items())
        return "{}({})".format(self.__class__.__name__, ", ".join(items))
