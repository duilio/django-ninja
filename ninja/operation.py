import pydantic
import django
from django.http import HttpResponse, HttpResponseNotAllowed
from typing import Callable, List, Any, Union, Optional, Sequence
from ninja.responses import Response
from ninja.errors import InvalidInput, ConfigError
from ninja.constants import NOT_SET
from ninja.schema import Schema
from ninja.signature import ViewSignature, is_async
from ninja.utils import check_csrf


class Operation:
    def __init__(
        self,
        path: str,
        methods: List[str],
        view_func: Callable,
        *,
        auth: Optional[Union[Sequence[Callable], Callable, object]] = NOT_SET,
        response: Any = None,
    ):
        self.is_async = False
        self.path: str = path
        self.methods: List[str] = methods
        self.view_func: Callable = view_func
        self.api = None

        self.auth_param: Optional[Union[Sequence[Callable], Callable, object]] = auth
        self.auth_callbacks: Sequence[Callable] = []
        self._set_auth(auth)

        self.signature = ViewSignature(self.path, self.view_func)
        self.models = self.signature.models

        if isinstance(response, dict):
            self.response_model = self._create_response_model_multiple(response)
        else:
            self.response_model = self._create_response_model(response)

    def run(self, request, **kw):
        error = self._run_checks(request)
        if error:
            return error

        values, errors = self._get_values(request, kw)
        if errors:
            return Response({"detail": errors}, status=422)
        result = self.view_func(request, **values)
        return self._create_response(result)

    def set_api_instance(self, api):
        self.api = api
        if self.auth_param == NOT_SET and api.auth != NOT_SET:
            # if api instance have auth and operation not - then we set auth from api instance
            self._set_auth(self.api.auth)

    def _set_auth(self, auth: Optional[Union[Sequence[Callable], Callable, object]]):
        if auth is not None and auth is not NOT_SET:
            self.auth_callbacks = isinstance(auth, Sequence) and auth or [auth]

    def _run_checks(self, request):
        "Runs security checks for each operation"
        # auth:
        if self.auth_callbacks:
            error = self._run_authentication(request)
            if error:
                return error

        # csrf:
        if self.api.csrf:
            error = check_csrf(request, self.view_func)
            if error:
                return error

    def _run_authentication(self, request):
        for callback in self.auth_callbacks:
            result = callback(request)
            if result is not None:
                request.auth = result
                return
        return Response({"detail": "Unauthorized"}, status=401)

    def _create_response(self, result: Any):
        if isinstance(result, HttpResponse):
            return result
        if self.response_model is None:
            return Response(result)

        status = 200
        response_model = self.response_model
        if isinstance(result, tuple) and len(result) == 2:
            status = result[0]
            result = result[1]
        if isinstance(response_model, dict):
            if status not in response_model.keys():
                raise ConfigError(f"Schema for status {status} is not set in response")
            response_model = response_model[status]

        resp_object = ResponseObject(result)
        # ^ we need object because getter_dict seems work only with from_orm
        result = response_model.from_orm(resp_object).dict()["response"]
        return Response(result, status=status)

    def _get_values(self, request, path_params):
        values, errors = {}, []
        for model in self.models:
            try:
                data = model.resolve(request, path_params)
                values.update(data)
            except (pydantic.ValidationError, InvalidInput) as e:
                items = []
                for i in e.errors():
                    i["loc"] = (model._in,) + i["loc"]
                    items.append(i)
                errors.extend(items)
        return values, errors

    def _create_response_model_multiple(self, response_param):
        # TODO: do not modify response_param, return copy instead
        for status, model in response_param.items():
            response_param[status] = self._create_response_model(model)
        return response_param

    def _create_response_model(self, response_param):
        if response_param is None:
            return
        attrs = {"__annotations__": {"response": response_param}}
        return type("NinjaResponseSchema", (Schema,), attrs)


class AsyncOperation(Operation):
    def __init__(self, *args, **kwargs):
        if django.VERSION < (3, 1):  # pragma: no cover
            raise Exception("Async operations are supported only with Django 3.1+")
        super().__init__(*args, **kwargs)
        self.is_async = True

    async def run(self, request, **kw):
        error = self._run_checks(request)
        if error:
            return error

        values, errors = self._get_values(request, kw)
        if errors:
            return Response({"detail": errors}, status=422)
        result = await self.view_func(request, **values)
        return self._create_response(result)


class PathView:
    def __init__(self):
        self.operations = []
        self.is_async = False  # if at least one operation is async - will become True

    def add(
        self,
        path: str,
        methods: List[str],
        view_func: Callable,
        *,
        auth: Optional[Union[Sequence[Callable], Callable, object]] = NOT_SET,
        response=None,
    ):
        if is_async(view_func):
            self.is_async = True
            operation = AsyncOperation(
                path, methods, view_func, auth=auth, response=response
            )
        else:
            operation = Operation(
                path, methods, view_func, auth=auth, response=response
            )

        self.operations.append(operation)
        return operation

    def set_api_instance(self, api):
        self.api = api
        for op in self.operations:
            op.set_api_instance(api)

    def get_view(self):
        if self.is_async:
            view = self._async_view
        else:
            view = self._sync_view

        view.__func__.csrf_exempt = True
        return view

    def _sync_view(self, request, *a, **kw):
        operation, error = self._find_operation(request)
        if error:
            return error
        return operation.run(request, *a, **kw)

    async def _async_view(self, request, *a, **kw):
        from asgiref.sync import sync_to_async

        operation, error = self._find_operation(request)
        if error:
            return error
        if operation.is_async:
            return await operation.run(request, *a, **kw)
        else:
            return await sync_to_async(operation.run)(request, *a, **kw)

    def _find_operation(self, request):
        allowed_methods = set()
        for op in self.operations:
            allowed_methods.update(op.methods)
            if request.method in op.methods:
                return op, None
        return (
            None,
            HttpResponseNotAllowed(allowed_methods, content=b"Method not allowed"),
        )


class ResponseObject(object):
    "Basically this is just a helper to be able to pass response to pydantic's from_orm"

    def __init__(self, response):
        self.response = response
