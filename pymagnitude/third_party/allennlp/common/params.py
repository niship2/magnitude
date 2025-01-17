u"""
The :class:`~allennlp.common.params.Params` class represents a dictionary of
parameters (e.g. for configuring a model), with added functionality around
logging and validation.
"""


from __future__ import with_statement
from __future__ import absolute_import
# typing
# from collections import MutableMapping, OrderedDict
from collections import OrderedDict
from collections.abc import MutableMapping
import copy
import json
import logging
import os

# overrides
from io import open

# _jsonnet doesn't work on Windows, so we have to use fakes.
try:
    from _jsonnet import evaluate_file, evaluate_snippet
except ImportError:
    def evaluate_file(filename, **_kwargs):
        logger.warning("_jsonnet not loaded, treating {filename} as json")
        with open(filename, u'r') as evaluation_file:
            return evaluation_file.read()

    def evaluate_snippet(_filename, expr, **_kwargs):
        logger.warning("_jsonnet not loaded, treating snippet as json")
        return expr

from allennlp.common.checks import ConfigurationError
from allennlp.common.file_utils import cached_path

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def unflatten(flat_dict):
    u"""
    Given a "flattened" dict with compound keys, e.g.
        {"a.b": 0}
    unflatten it:
        {"a": {"b": 0}}
    """
    unflat = {}

    for compound_key, value in list(flat_dict.items()):
        curr_dict = unflat
        parts = compound_key.split(u".")
        for key in parts[:-1]:
            curr_value = curr_dict.get(key)
            if key not in curr_dict:
                curr_dict[key] = {}
                curr_dict = curr_dict[key]
            elif isinstance(curr_value, dict):
                curr_dict = curr_value
            else:
                raise ConfigurationError(u"flattened dictionary is invalid")
        if not isinstance(curr_dict, dict) or parts[-1] in curr_dict:
            raise ConfigurationError(u"flattened dictionary is invalid")
        else:
            curr_dict[parts[-1]] = value

    return unflat


def with_fallback(preferred, fallback):
    u"""
    Deep merge two dicts, preferring values from `preferred`.
    """
    preferred_keys = set(preferred.keys())
    fallback_keys = set(fallback.keys())
    common_keys = preferred_keys & fallback_keys

    merged = {}

    for key in preferred_keys - fallback_keys:
        merged[key] = copy.deepcopy(preferred[key])
    for key in fallback_keys - preferred_keys:
        merged[key] = copy.deepcopy(fallback[key])

    for key in common_keys:
        preferred_value = preferred[key]
        fallback_value = fallback[key]

        if isinstance(preferred_value, dict) and isinstance(fallback_value, dict):
            merged[key] = with_fallback(preferred_value, fallback_value)
        else:
            merged[key] = copy.deepcopy(preferred_value)

    return merged


def parse_overrides(serialized_overrides):
    if serialized_overrides:
        ext_vars = dict(os.environ)
        return unflatten(json.loads(evaluate_snippet(u"", serialized_overrides, ext_vars=ext_vars)))
    else:
        return {}


class Params(MutableMapping):
    u"""
    Represents a parameter dictionary with a history, and contains other functionality around
    parameter passing and validation for AllenNLP.

    There are currently two benefits of a ``Params`` object over a plain dictionary for parameter
    passing:

    #. We handle a few kinds of parameter validation, including making sure that parameters
       representing discrete choices actually have acceptable values, and making sure no extra
       parameters are passed.
    #. We log all parameter reads, including default values.  This gives a more complete
       specification of the actual parameters used than is given in a JSON file, because
       those may not specify what default values were used, whereas this will log them.

    The convention for using a ``Params`` object in AllenNLP is that you will consume the parameters
    as you read them, so that there are none left when you've read everything you expect.  This
    lets us easily validate that you didn't pass in any `extra` parameters, just by making sure
    that the parameter dictionary is empty.  You should do this when you're done handling
    parameters, by calling :func:`Params.assert_empty`.
    """

    # This allows us to check for the presence of "None" as a default argument,
    # which we require because we make a distinction bewteen passing a value of "None"
    # and passing no value to the default parameter of "pop".
    DEFAULT = object()

    def __init__(self,
                 params,
                 history=u"",
                 loading_from_archive=False,
                 files_to_archive=None):
        self.params = _replace_none(params)
        self.history = history
        self.loading_from_archive = loading_from_archive
        self.files_to_archive = {} if files_to_archive is None else files_to_archive

    def add_file_to_archive(self, name):
        u"""
        Any class in its ``from_params`` method can request that some of its
        input files be added to the archive by calling this method.

        For example, if some class ``A`` had an ``input_file`` parameter, it could call

        ```
        params.add_file_to_archive("input_file")
        ```

        which would store the supplied value for ``input_file`` at the key
        ``previous.history.and.then.input_file``. The ``files_to_archive`` dict
        is shared with child instances via the ``_check_is_dict`` method, so that
        the final mapping can be retrieved from the top-level ``Params`` object.

        NOTE: You must call ``add_file_to_archive`` before you ``pop()``
        the parameter, because the ``Params`` instance looks up the value
        of the filename inside itself.

        If the ``loading_from_archive`` flag is True, this will be a no-op.
        """
        if not self.loading_from_archive:
            self.files_to_archive["{self.history}{name}"] = cached_path(
                self.get(name))

    # overrides
    def pop(self, key, default=DEFAULT):
        u"""
        Performs the functionality associated with dict.pop(key), along with checking for
        returned dictionaries, replacing them with Param objects with an updated history.

        If ``key`` is not present in the dictionary, and no default was specified, we raise a
        ``ConfigurationError``, instead of the typical ``KeyError``.
        """
        if default is self.DEFAULT:
            try:
                value = self.params.pop(key)
            except KeyError:
                raise ConfigurationError(
                    u"key \"{}\" is required at location \"{}\"".format(key, self.history))
        else:
            value = self.params.pop(key, default)
        if not isinstance(value, dict):
            logger.info(self.history + key + u" = " +
                        unicode(value))  # type: ignore
        return self._check_is_dict(key, value)

    def pop_int(self, key, default=DEFAULT):
        u"""
        Performs a pop and coerces to an int.
        """
        value = self.pop(key, default)
        if value is None:
            return None
        else:
            return int(value)

    def pop_float(self, key, default=DEFAULT):
        u"""
        Performs a pop and coerces to a float.
        """
        value = self.pop(key, default)
        if value is None:
            return None
        else:
            return float(value)

    def pop_bool(self, key, default=DEFAULT):
        u"""
        Performs a pop and coerces to a bool.
        """
        value = self.pop(key, default)
        if value is None:
            return None
        elif isinstance(value, bool):
            return value
        elif value == u"true":
            return True
        elif value == u"false":
            return False
        else:
            raise ValueError(u"Cannot convert variable to bool: " + value)

    # overrides
    def get(self, key, default=DEFAULT):
        u"""
        Performs the functionality associated with dict.get(key) but also checks for returned
        dicts and returns a Params object in their place with an updated history.
        """
        if default is self.DEFAULT:
            try:
                value = self.params.get(key)
            except KeyError:
                raise ConfigurationError(
                    u"key \"{}\" is required at location \"{}\"".format(key, self.history))
        else:
            value = self.params.get(key, default)
        return self._check_is_dict(key, value)

    def pop_choice(self, key, choices, default_to_first_choice=False):
        u"""
        Gets the value of ``key`` in the ``params`` dictionary, ensuring that the value is one of
        the given choices. Note that this `pops` the key from params, modifying the dictionary,
        consistent with how parameters are processed in this codebase.

        Parameters
        ----------
        key: str
            Key to get the value from in the param dictionary
        choices: List[Any]
            A list of valid options for values corresponding to ``key``.  For example, if you're
            specifying the type of encoder to use for some part of your model, the choices might be
            the list of encoder classes we know about and can instantiate.  If the value we find in
            the param dictionary is not in ``choices``, we raise a ``ConfigurationError``, because
            the user specified an invalid value in their parameter file.
        default_to_first_choice: bool, optional (default=False)
            If this is ``True``, we allow the ``key`` to not be present in the parameter
            dictionary.  If the key is not present, we will use the return as the value the first
            choice in the ``choices`` list.  If this is ``False``, we raise a
            ``ConfigurationError``, because specifying the ``key`` is required (e.g., you `have` to
            specify your model class when running an experiment, but you can feel free to use
            default settings for encoders if you want).
        """
        default = choices[0] if default_to_first_choice else self.DEFAULT
        value = self.pop(key, default)
        if value not in choices:
            key_str = self.history + key
            message = u'%s not in acceptable choices for %s: %s' % (
                value, key_str, unicode(choices))
            raise ConfigurationError(message)
        return value

    def as_dict(self, quiet=False):
        u"""
        Sometimes we need to just represent the parameters as a dict, for instance when we pass
        them to a Keras layer(so that they can be serialised).

        Parameters
        ----------
        quiet: bool, optional (default = False)
            Whether to log the parameters before returning them as a dict.
        """
        if quiet:
            return self.params

        def log_recursively(parameters, history):
            for key, value in list(parameters.items()):
                if isinstance(value, dict):
                    new_local_history = history + key + u"."
                    log_recursively(value, new_local_history)
                else:
                    logger.info(history + key + u" = " + unicode(value))

        logger.info(u"Converting Params object to dict; logging of default "
                    u"values will not occur when dictionary parameters are "
                    u"used subsequently.")
        logger.info(u"CURRENTLY DEFINED PARAMETERS: ")
        log_recursively(self.params, self.history)
        return self.params

    def as_flat_dict(self):
        u"""
        Returns the parameters of a flat dictionary from keys to values.
        Nested structure is collapsed with periods.
        """
        flat_params = {}

        def recurse(parameters, path):
            for key, value in list(parameters.items()):
                newpath = path + [key]
                if isinstance(value, dict):
                    recurse(value, newpath)
                else:
                    flat_params[u'.'.join(newpath)] = value

        recurse(self.params, [])
        return flat_params

    def duplicate(self):
        u"""
        Uses ``copy.deepcopy()`` to create a duplicate (but fully distinct)
        copy of these Params.
        """
        return Params(copy.deepcopy(self.params))

    def assert_empty(self, class_name):
        u"""
        Raises a ``ConfigurationError`` if ``self.params`` is not empty.  We take ``class_name`` as
        an argument so that the error message gives some idea of where an error happened, if there
        was one.  ``class_name`` should be the name of the `calling` class, the one that got extra
        parameters (if there are any).
        """
        if self.params:
            raise ConfigurationError(
                u"Extra parameters passed to {}: {}".format(class_name, self.params))

    def __getitem__(self, key):
        if key in self.params:
            return self._check_is_dict(key, self.params[key])
        else:
            raise KeyError

    def __setitem__(self, key, value):
        self.params[key] = value

    def __delitem__(self, key):
        del self.params[key]

    def __iter__(self):
        return iter(self.params)

    def __len__(self):
        return len(self.params)

    def _check_is_dict(self, new_history, value):
        if isinstance(value, dict):
            new_history = self.history + new_history + u"."
            return Params(value,
                          history=new_history,
                          loading_from_archive=self.loading_from_archive,
                          files_to_archive=self.files_to_archive)
        if isinstance(value, list):
            value = [self._check_is_dict(
                new_history + u'.list', v) for v in value]
        return value

    @staticmethod
    def from_file(params_file, params_overrides=u""):
        u"""
        Load a `Params` object from a configuration file.
        """
        # redirect to cache, if necessary
        params_file = cached_path(params_file)
        ext_vars = dict(os.environ)

        file_dict = json.loads(evaluate_file(params_file, ext_vars=ext_vars))

        overrides_dict = parse_overrides(params_overrides)
        param_dict = with_fallback(preferred=overrides_dict, fallback=file_dict)

        return Params(param_dict)

    def to_file(self, params_file, preference_orders=None):
        with open(params_file, u"w") as handle:
            json.dump(self.as_ordered_dict(preference_orders), handle, indent=4)

    def as_ordered_dict(self, preference_orders=None):
        u"""
        Returns Ordered Dict of Params from list of partial order preferences.

        Parameters
        ----------
        preference_orders: List[List[str]], optional
            ``preference_orders`` is list of partial preference orders. ["A", "B", "C"] means
            "A" > "B" > "C". For multiple preference_orders first will be considered first.
            Keys not found, will have last but alphabetical preference. Default Preferences:
            ``[["dataset_reader", "iterator", "model", "train_data_path", "validation_data_path",
            "test_data_path", "trainer", "vocabulary"], ["type"]]``
        """
        params_dict = self.as_dict(quiet=True)
        if not preference_orders:
            preference_orders = []
            preference_orders.append([u"dataset_reader", u"iterator", u"model",
                                      u"train_data_path", u"validation_data_path", u"test_data_path",
                                      u"trainer", u"vocabulary"])
            preference_orders.append([u"type"])

        def order_func(key):
            # Makes a tuple to use for ordering.  The tuple is an index into each of the `preference_orders`,
            # followed by the key itself.  This gives us integer sorting if you have a key in one of the
            # `preference_orders`, followed by alphabetical ordering if not.
            order_tuple = [order.index(key) if key in order else len(
                order) for order in preference_orders]
            return order_tuple + [key]

        def order_dict(dictionary, order_func):
            # Recursively orders dictionary according to scoring order_func
            result = OrderedDict()
            for key, val in sorted(list(dictionary.items()), key=lambda item: order_func(item[0])):
                result[key] = order_dict(
                    val, order_func) if isinstance(val, dict) else val
            return result

        return order_dict(params_dict, order_func)


def pop_choice(params,
               key,
               choices,
               default_to_first_choice=False,
               history=u"?."):
    u"""
    Performs the same function as :func:`Params.pop_choice`, but is required in order to deal with
    places that the Params object is not welcome, such as inside Keras layers.  See the docstring
    of that method for more detail on how this function works.

    This method adds a ``history`` parameter, in the off-chance that you know it, so that we can
    reproduce :func:`Params.pop_choice` exactly.  We default to using "?." if you don't know the
    history, so you'll have to fix that in the log if you want to actually recover the logged
    parameters.
    """
    value = Params(params, history).pop_choice(
        key, choices, default_to_first_choice)
    return value


def _replace_none(dictionary):
    for key in list(dictionary.keys()):
        if dictionary[key] == u"None":
            dictionary[key] = None
        elif isinstance(dictionary[key], dict):
            dictionary[key] = _replace_none(dictionary[key])
    return dictionary
