import inspect
from types import CodeType, FunctionType
from typing import Any, Optional, List, Callable, Union
import torch

from .node import Argument
from .graph import Graph
from .graph_module import GraphModule
from .proxy import TracerBase

HAS_VARSTUFF = inspect.CO_VARARGS | inspect.CO_VARKEYWORDS

def _patch_function(fn: FunctionType, nargs: int) -> FunctionType:
    co = fn.__code__
    co_flags = co.co_flags & ~HAS_VARSTUFF
    co_args : tuple
    if hasattr(co, "co_posonlyargcount"):
        co_args = (
            nargs, 0,
            0, co.co_nlocals, co.co_stacksize,
            co_flags, co.co_code, co.co_consts, co.co_names,
            co.co_varnames, co.co_filename, co.co_name,
            co.co_firstlineno, co.co_lnotab, co.co_freevars,
            co.co_cellvars
        )
    else:
        co_args = (
            nargs, 0, co.co_nlocals,
            co.co_stacksize, co_flags, co.co_code, co.co_consts,
            co.co_names, co.co_varnames, co.co_filename,
            co.co_name, co.co_firstlineno, co.co_lnotab,
            co.co_freevars, co.co_cellvars)
    new_code = CodeType(*co_args)  # type: ignore
    return FunctionType(new_code, fn.__globals__, fn.__name__, fn.__defaults__, fn.__closure__)

    # we need to insert placeholder nodes for *args and **kwargs
    # we can't call this function normally, otherwise it would try to unpack them
    # instead, let's make python think that args and kwargs are normal variables

class Tracer(TracerBase):
    def __init__(self):
        super().__init__()

    def create_arg(self, a: Any) -> Argument:
        # The base tracer is used to construct Graphs when there is no associated
        # module hierarchy, so it can never create parameter references.
        # The default tracer adds the ability to refer to parameters when
        # tracing modules.
        if isinstance(a, torch.nn.Parameter):
            for n, p in self.root.named_parameters():
                if a is p:
                    return self.create_node('get_attr', n, (), {})
            raise NameError('parameter is not a member of this module')
        # Tensors do not have a reliable string repr() from which they can be
        # constructed (and we probably don't want to rely on that, either), so
        # for any constant Tensor values we encounter, first search for if they
        # are an attribute of some module in the module hierarchy. If so, emit
        # a get_attr to retrieve that tensor. Otherwise, we'll store away the
        # tensor value into a special attribute on the Module s.t. we can
        # retrieve it with a get_attr.
        if isinstance(a, torch.Tensor):
            # TODO: slow
            def search_for_tensor(m : torch.nn.Module) -> Optional[List[str]]:
                """
                Search for a tensor value in the module's attributes. If it's
                found, return the qualified name of that attribute, given the
                previous `qualname_atoms`. If it's not found, recurse down into
                child submodules. If it's not found there, return None
                """
                for n, p in m.__dict__.items():
                    if a is p:
                        return [n]
                for n, c in m.named_children():
                    maybe_result : Optional[List[str]] = search_for_tensor(c)
                    if maybe_result:
                        return [n] + maybe_result
                return None
            # Retrieve the qualname for an existing Tensor attribute
            qualname_atoms : Optional[List[str]] = search_for_tensor(self.root)
            qualname = '.'.join(qualname_atoms) if qualname_atoms else None

            # Tensor was not found in the Module hierarchy, stow it away in a
            # special attribute and set the qualname to refer to that
            if not qualname:
                i = 0
                while True:
                    qualname = f'__tensor_constant{i}'
                    if not hasattr(self.root, qualname):
                        break
                    i += 1
                setattr(self.root, qualname, a)

            return self.create_node('get_attr', qualname, (), {})
        return super().create_arg(a)

    def is_leaf_module(self, m: torch.nn.Module, module_qualified_name : str) -> bool:
        """
        A method to specify whether a given `nn.Module` is a "leaf" module.

        Leaf modules are the atomic units that appear in
        the IR, referenced by `call_module` calls. By default,
        Modules in the PyTorch standard library namespace (torch.nn)
        are leaf modules. All other modules are traced through and
        their constituent ops are recorded, unless specified otherwise
        via this parameter.

        Args
        m - The module itself
        module_qualified_name - The path to root of this module. For example,
            if you have a module hierarchy where submodule `foo` contains
            submodule `bar`, which contains submodule `baz`, that module will
            appear with the qualified name `foo.bar.baz` here.
        """
        return m.__module__.startswith('torch.nn') and not isinstance(m, torch.nn.Sequential)

    def path_of_module(self, mod):
        for n, p in self.root.named_modules():
            if mod is p:
                return n
        raise NameError('module is not installed as a submodule')

    def call_module(self, m: torch.nn.Module, forward: Callable[..., Any], args, kwargs):
        module_qualified_name = self.path_of_module(m)
        if not self.is_leaf_module(m, module_qualified_name):
            return forward(*args, **kwargs)
        return self.create_proxy('call_module', module_qualified_name, args, kwargs)

    def create_args_for_root(self, root_fn, is_module):
        # In some cases, a function or method has been decorated with a wrapper
        # defined via `functools.wraps`. In this case, the outer code object
        # will likely not contain the actual parameters we care about, so unwrap
        # the function to get to the innermost callable.
        fn_for_analysis = inspect.unwrap(root_fn)
        co = fn_for_analysis.__code__
        total_args = co.co_argcount + co.co_kwonlyargcount
        names_iter = iter(co.co_varnames)
        args : List[Any] = []
        skip_arg_idx = 0
        if is_module:
            if total_args == 0:
                raise RuntimeError('`self` argument cannot be part of *args expansion!')
            skip_arg_idx = 1
            next(names_iter)  # skip self
            args.append(self.root)

        def proxy_placeholder(name: str):
            return self.create_proxy('placeholder', name, (), {},
                                     type_expr=fn_for_analysis.__annotations__.get(name, None))

        args.extend(proxy_placeholder(next(names_iter)) for _ in range(skip_arg_idx, total_args))

        if co.co_kwonlyargcount > 0 or co.co_flags & HAS_VARSTUFF:
            # TODO: type annotations for *args and **kwargs
            if co.co_flags & inspect.CO_VARARGS:
                args.append(proxy_placeholder('*' + next(names_iter)))
            if co.co_flags & inspect.CO_VARKEYWORDS:
                args.append(proxy_placeholder('**' + next(names_iter)))
            root_fn = _patch_function(root_fn, len(args))

        return root_fn, args

    def trace(self, root: Union[torch.nn.Module, Callable]) -> Graph:
        if isinstance(root, torch.nn.Module):
            self.root = root
            fn = type(root).forward
        else:
            self.root = torch.nn.Module()
            fn = root
        self.graph = Graph()

        assert isinstance(fn, FunctionType)

        fn, args = self.create_args_for_root(fn, isinstance(root, torch.nn.Module))

        orig_call = torch.nn.Module.__call__

        def module_call_wrapper(mod, *args, **kwargs):
            def forward(*args, **kwargs):
                return orig_call(mod, *args, **kwargs)

            return self.call_module(mod, forward, args, kwargs)

        try:
            torch.nn.Module.__call__ = module_call_wrapper
            self.create_node('output', 'output', (self.create_arg(fn(*args)),), {},
                             type_expr=fn.__annotations__.get('return', None))
        finally:
            torch.nn.Module.__call__ = orig_call
        return self.graph

# Symbolic tracing API
#
# Given an `nn.Module` or function instance `root`, this function will return a `GraphModule`
# constructed by recording operations seen while tracing through `root`.
#
# Args:
#   - root - the `nn.Module` instance to trace
def symbolic_trace(root : Union[torch.nn.Module, Callable]) -> GraphModule:
    return GraphModule(root if isinstance(root, torch.nn.Module) else torch.nn.Module(), Tracer().trace(root))
