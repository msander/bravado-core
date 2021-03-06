# -*- coding: utf-8 -*-
import functools
import logging
import warnings

from jsonschema import FormatChecker
from jsonschema.validators import RefResolver
from six import iteritems
from six.moves.urllib import parse as urlparse
from swagger_spec_validator import validator20
from swagger_spec_validator.ref_validators import attach_scope, in_scope

from bravado_core import formatter
from bravado_core.exception import SwaggerSchemaError, SwaggerValidationError
from bravado_core.formatter import return_true_wrapper
from bravado_core.model import tag_models, collect_models
from bravado_core.resource import build_resources
from bravado_core.schema import is_dict_like, is_list_like, is_ref


log = logging.getLogger(__name__)


CONFIG_DEFAULTS = {
    # On the client side, validate incoming responses
    # On the server side, validate outgoing responses
    'validate_responses': True,

    # On the client side, validate outgoing requests
    # On the server side, validate incoming requests
    'validate_requests': True,

    # Use swagger_spec_validator to validate the swagger spec
    'validate_swagger_spec': True,

    # Use Python classes (models) instead of dicts for #/definitions/{models}
    # On the client side, this applies to incoming responses.
    # On the server side, this applies to incoming requests.
    #
    # NOTE: outgoing requests on the client side and outgoing responses on the
    #       server side can use either models or dicts.
    'use_models': True,

    # List of user-defined formats of type
    # :class:`bravado_core.formatter.SwaggerFormat`. These formats are in
    # addition to the formats already supported by the Swagger 2.0
    # Specification.
    'formats': []
}


class Spec(object):
    """Represents a Swagger Specification for a service.

    :param spec_dict: Swagger API specification in json-like dict form
    :param origin_url: URL from which the spec was retrieved.
    :param http_client: Used to retrive the spec via http/https.
    :type http_client: :class:`bravado.http_client.HTTPClient`
    :param config: Configuration dict. See CONFIG_DEFAULTS.
    """
    def __init__(self, spec_dict, origin_url=None, http_client=None,
                 config=None):
        self.spec_dict = spec_dict
        self.origin_url = origin_url
        self.http_client = http_client
        self.api_url = None
        self.config = dict(CONFIG_DEFAULTS, **(config or {}))

        # (key, value) = (simple format def name, Model type)
        # (key, value) = (#/ format def ref, Model type)
        self.definitions = {}

        # (key, value) = (simple resource name, Resource)
        # (key, value) = (#/ format resource ref, Resource)
        self.resources = None

        # (key, value) = (simple ref name, param_spec in dict form)
        # (key, value) = (#/ format ref name, param_spec in dict form)
        self.params = None

        # Built on-demand - see get_op_for_request(..)
        self._request_to_op_map = None

        # (key, value) = (format name, SwaggerFormat)
        self.user_defined_formats = {}
        self.format_checker = FormatChecker()

        self.resolver = RefResolver(
            base_uri=origin_url or '',
            referrer=self.spec_dict,
            handlers=build_http_handlers(http_client))

    @classmethod
    def from_dict(cls, spec_dict, origin_url=None, http_client=None,
                  config=None):
        """Build a :class:`Spec` from Swagger API Specificiation

        :param spec_dict: swagger spec in json-like dict form.
        :param origin_url: the url used to retrieve the spec, if any
        :type  origin_url: str
        :param config: Configuration dict. See CONFIG_DEFAULTS.
        """
        spec = cls(spec_dict, origin_url, http_client, config)
        spec.build()
        return spec

    def build(self):
        if self.config['validate_swagger_spec']:
            self.resolver = validator20.validate_spec(
                self.spec_dict, spec_url=self.origin_url or '',
                http_handlers=build_http_handlers(self.http_client))

        post_process_spec(
            self,
            on_container_callbacks=[
                functools.partial(
                    tag_models, visited_models={}, swagger_spec=self),
                functools.partial(
                    collect_models, models=self.definitions,
                    swagger_spec=self)
            ])

        for format in self.config['formats']:
            self.register_format(format)

        self.api_url = build_api_serving_url(self.spec_dict, self.origin_url)
        self.resources = build_resources(self)

    def deref(self, ref_dict):
        """Dereference ref_dict (if it is indeed a ref) and return what the
        ref points to.

        :param ref_dict:  {'$ref': '#/blah/blah'}
        :return: dereferenced value of ref_dict
        :rtype: scalar, list, dict
        """
        if ref_dict is None or not is_ref(ref_dict):
            return ref_dict

        # Restore attached resolution scope before resolving since the
        # resolver doesn't have a traversal history (accumulated scope_stack)
        # when asked to resolve.
        with in_scope(self.resolver, ref_dict):
            log.debug('Resolving {0} with scope {1}: {2}'.format(
                ref_dict['$ref'],
                len(self.resolver._scopes_stack),
                self.resolver._scopes_stack))

            _, target = self.resolver.resolve(ref_dict['$ref'])
            return target

    def get_op_for_request(self, http_method, path_pattern):
        """Return the Swagger operation for the passed in request http method
        and path pattern. Makes it really easy for server-side implementations
        to map incoming requests to the Swagger spec.

        :param http_method: http method of the request
        :param path_pattern: request path pattern. e.g. /foo/{bar}/baz/{id}

        :returns: the matching operation or None if a match couldn't be found
        :rtype: :class:`bravado_core.operation.Operation`
        """
        if self._request_to_op_map is None:
            # lazy initialization
            self._request_to_op_map = {}
            base_path = self.spec_dict.get('basePath', '').rstrip('/')
            for resource in self.resources.values():
                for op in resource.operations.values():
                    full_path = base_path + op.path_name
                    key = (op.http_method, full_path)
                    self._request_to_op_map[key] = op

        key = (http_method.lower(), path_pattern)
        return self._request_to_op_map.get(key)

    def register_format(self, user_defined_format):
        """Registers a user-defined format to be used with this spec.

        :type user_defined_format:
            :class:`bravado_core.formatter.SwaggerFormat`
        """
        name = user_defined_format.format
        self.user_defined_formats[name] = user_defined_format
        validate = return_true_wrapper(user_defined_format.validate)
        self.format_checker.checks(
            name, raises=(SwaggerValidationError,))(validate)

    def get_format(self, name):
        """
        :param name: Name of the format to retrieve
        :rtype: :class:`bravado_core.formatters.SwaggerFormat`
        """
        if name in formatter.DEFAULT_FORMATS:
            return formatter.DEFAULT_FORMATS[name]
        format = self.user_defined_formats.get(name)
        if format is None:
            warnings.warn('{0} format is not registered with bravado-core!'
                          .format(name), Warning)
        return format


def build_http_handlers(http_client):
    """Create a mapping of uri schemes to callables that take a uri. The
    callable is used by jsonschema's RefResolver to download remote $refs.

    :param http_client: http_client with a request() method

    :returns: dict like {'http': callable, 'https': callable)
    """
    def download(uri):
        log.debug('Downloading {0}'.format(uri))
        request_params = {
            'method': 'GET',
            'url': uri,
        }
        return http_client.request(request_params).result().json()

    return {
        'http': download,
        'https': download,
    }


def build_api_serving_url(spec_dict, origin_url=None, preferred_scheme=None):
    """The URL used to service API requests does not necessarily have to be the
    same URL that was used to retrieve the API spec_dict.

    The existence of three fields in the root of the specification govern
    the value of the api_serving_url:

    - host string
        The host (name or ip) serving the API. This MUST be the host only and
        does not include the scheme nor sub-paths. It MAY include a port.
        If the host is not included, the host serving the documentation is to
        be used (including the port). The host does not support path templating.

    - basePath string
        The base path on which the API is served, which is relative to the
        host. If it is not included, the API is served directly under the host.
        The value MUST start with a leading slash (/). The basePath does not
        support path templating.

    - schemes [string]
        The transfer protocol of the API. Values MUST be from the list:
        "http", "https", "ws", "wss". If the schemes is not included,
        the default scheme to be used is the one used to access the
        specification.

    See https://github.com/swagger-api/swagger-spec_dict/blob/master/versions/2.0.md#swagger-object-   # noqa

    :param spec_dict: the Swagger spec in json-like dict form
    :param origin_url: the URL from which the spec was retrieved, if any. This
        is only used in Swagger clients.
    :param preferred_scheme: preferred scheme to use if more than one scheme is
        supported by the API.
    :return: base url which services api requests
    :raises: SwaggerSchemaError
    """
    origin_url = origin_url or 'http://localhost/'
    origin = urlparse.urlparse(origin_url)

    def pick_a_scheme(schemes):
        if not schemes:
            return origin.scheme

        if preferred_scheme:
            if preferred_scheme in schemes:
                return preferred_scheme
            raise SwaggerSchemaError(
                "Preferred scheme {0} not supported by API. Available schemes "
                "include {1}".format(preferred_scheme, schemes))

        if origin.scheme in schemes:
            return origin.scheme

        if len(schemes) == 1:
            return schemes[0]

        raise SwaggerSchemaError(
            "Origin scheme {0} not supported by API. Available schemes "
            "include {1}".format(origin.scheme, schemes))

    netloc = spec_dict.get('host', origin.netloc)
    path = spec_dict.get('basePath', origin.path)
    scheme = pick_a_scheme(spec_dict.get('schemes'))
    return urlparse.urlunparse((scheme, netloc, path, None, None, None))


def post_process_spec(swagger_spec, on_container_callbacks):
    """Post-process the passed in spec_dict.

    For each container type (list or dict) that is traversed in spec_dict,
    the list of passed in callbacks is called with arguments (container, key).

    When the container is a dict, key is obviously the key for the value being
    traversed.

    When the container is a list, key is an integer index into the list of the
    value being traversed.

    In addition to firing the passed in callbacks, $refs are annotated with
    an 'x-scope' key that contains the current scope_stack of the RefResolver.
    The 'x-scope' scope_stack is used during request/response marshalling to
    assume a given scope before de-reffing $refs (otherwise, de-reffing won't
    work).

    :type swagger_spec: :class:`bravado_core.spec.Spec`
    :param on_container_callbacks: list of callbacks to be invoked on each
        container type.
    """
    def fire_callbacks(container, key, path):
        for callback in on_container_callbacks:
            callback(container, key, path)

    resolver = swagger_spec.resolver

    def descend(fragment, path, visited_refs):

        if is_ref(fragment):
            ref_dict = fragment
            ref = fragment['$ref']
            attach_scope(ref_dict, resolver)

            # Don't recurse down already visited refs. A ref is not unique
            # by its name alone. Its scope (attached above) is part of the
            # equivalence comparison.
            if ref_dict in visited_refs:
                log.debug('Already visited %s' % ref)
                return

            visited_refs.append(ref_dict)
            with resolver.resolving(ref) as target:
                descend(target, path, visited_refs)
                return

        # fragment is guaranteed not to be a ref from this point onwards
        if is_dict_like(fragment):
            if 'type' not in fragment:
                if 'properties' in fragment:
                    fragment['type'] = 'object'
                elif 'items' in fragment:
                    fragment['type'] = 'array'
            for key, value in iteritems(fragment):
                fire_callbacks(fragment, key, path + [key])
                descend(fragment[key], path + [key], visited_refs)

        elif is_list_like(fragment):
            for index in range(len(fragment)):
                fire_callbacks(fragment, index, path + [str(index)])
                descend(fragment[index], path + [str(index)], visited_refs)

    descend(swagger_spec.spec_dict, path=[], visited_refs=[])
