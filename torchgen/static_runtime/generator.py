import json
import logging

import math
from typing import List, Optional, Sequence, Tuple, Union

import torchgen.api.cpp as cpp
from torchgen.context import native_function_manager
from torchgen.model import (
    Argument,
    BackendIndex,
    BaseTy,
    BaseType,
    FunctionSchema,
    NativeFunctionsGroup,
    NativeFunctionsViewGroup,
    OptionalType,
    SelfArgument,
    TensorOptionsArguments,
    Type,
)
from torchgen.static_runtime import config

logger: logger = logging.getLogger()


def has_alias(
    arguments: Sequence[Union[Argument, SelfArgument, TensorOptionsArguments]]
) -> bool:
    for arg in arguments:
        annotation = getattr(arg, "annotation", None)
        if not annotation:
            continue
        alias_set = getattr(annotation, "alias_set", ())
        if alias_set:
            return True
    return False


BLOCKED_OPS = frozenset(
    (
        # non cpu ops
        "sparse_sampled_addmm",
        "hspmm",
        # sparse ops
        "sspaddmm",
        "coalesce",
        "_indices",
        "indices",
        "_values",
        "values",
        "crow_indices",
        "col_indices",
        # deprecated ops
        "floor_divide",
        "ger",
        # buggy ops
        "conj_physical",  # P495807361
        "binary_cross_entropy",  # P496394764
        "arccosh",
        # uncommon ops
        "cholesky",
        "lu_solve",
        "linalg_cholesky",
        "linalg_householder_product",
        "linalg_ldl_solve",
        "_compute_linear_combination",
        # training related ops
        "_make_dual",
        # cannot call directly
        "_fw_primal",
        # no documentation
        "_index_reduce",
    )
)


def is_supported(g: Union[NativeFunctionsGroup, NativeFunctionsViewGroup]) -> bool:
    base_op_name = ""
    func = None
    if isinstance(g, NativeFunctionsViewGroup):
        base_op_name = g.view.root_name
        func = g.view.func
    else:
        base_op_name = g.out.func.name.name.base
        func = g.out.func
    if config.is_hand_written(g):
        logger.info(f"HAND WRITTEN: {base_op_name}")
        return False
    if base_op_name in BLOCKED_OPS:
        logger.info(f"BLOCKED: {base_op_name}")
        return False
    for arg in func.schema_order_arguments():
        maybe_method = ivalue_type_conversion_method(arg.type)
        if not maybe_method:
            # Type converting is unsupported yet.
            logger.info(f"NOT SUPPORTED TYPE CONVERTING: {str(func)}")
            return False

    if isinstance(g, NativeFunctionsViewGroup):
        if "at::Tensor" != cpp.returns_type(func.returns).cpp_type():
            # Returns a non-Tensor value.
            logger.info(f"NON-TENSOR RET TYPE: {str(func)}")
            return False
        return True

    # For out variant ops, we need to check the arguments of its functional func.
    for arg in g.functional.func.schema_order_arguments():
        maybe_method = ivalue_type_conversion_method(arg.type)
        if not maybe_method:
            # Type converting is unsupported yet.
            logger.info(f"NOT SUPPORTED TYPE CONVERTING: {str(g.functional.func)}")
            return False

    if not g.structured:
        # In case of unstructured op, we check if it has out variant implementation.
        # The out variant implementation satisfies the minimum requirement that it has the output tensor as the last
        # parameter.
        if (
            not hasattr(g, "out")
            or not str(func).endswith("Tensor(a!) out) -> Tensor(a!)")
            or not str(func.name).endswith(".out")
        ):
            return False
    if "at::Tensor &" != cpp.returns_type(func.returns).cpp_type():
        logger.info(f"NON_TENSOR RET TYPE: {str(func)}")
        return False
    if has_alias(func.arguments.non_out):
        # This op may create an alias of inputs.
        logger.info(f"INPUTS ALIAS: {base_op_name}")
        return False
    return True


def ivalue_type_conversion_method(
    arg_type: Union[BaseType, OptionalType, Type]
) -> Optional[Tuple[bool, str]]:
    """
    Return the method call expression of `c10::ivalue' to convert its contained value to
    the expected value of `arg_type` type. For example, for `arg_type` == BaseTy.Tensor,
    this function returns ".toTensor()", so that it can be appended to the ivalue's
    variable name to get the value of the expected type.
    """
    type_conversion_methods = {
        BaseTy.Tensor: ((True, "toTensor()"), (False, "toOptional<at::Tensor>()")),
        BaseTy.int: ((False, "toInt()"), (False, "toOptional<int64_t>()")),
        BaseTy.bool: ((False, "toBool()"), (False, "toOptional<bool>()")),
        BaseTy.Scalar: ((False, "toScalar()"), (False, "toOptional<at::Scalar>()")),
        BaseTy.ScalarType: (
            (False, "toScalarType()"),
            (False, "toOptional<at::ScalarType>()"),
        ),
        BaseTy.str: (
            (False, "toStringView()"),
            (False, "toOptional<c10::string_view>()"),
        ),
    }

    base_ty_object = None
    if isinstance(arg_type, BaseType):
        base_ty_object = arg_type.name
    elif isinstance(arg_type, OptionalType):
        if not isinstance(arg_type.elem, BaseType):
            # ListType is currently unsupported.
            return None
        base_ty_object = arg_type.elem.name
    else:
        return None

    if base_ty_object not in type_conversion_methods:
        return None
    methods = type_conversion_methods[base_ty_object]
    if isinstance(arg_type, BaseType):
        return methods[0]
    return methods[1]


should_use_int_tensor_ops_ = frozenset(
    (
        "bitwise_not",
        "bitwise_and",
        "bitwise_or",
        "bitwise_xor",
        "bitwise_left_shift",
        "bitwise_right_shift",
        "gcd",
        "lcm",
        "scatter",
        "gather",
        "_convert_indices_from_coo_to_csr",
        "_convert_indices_from_csr_to_coo",
    )
)
should_use_complex_tensor_ops_ = frozenset(("view_as_real", "imag", "_conj"))


def should_use_int_tensor(op_name: str) -> bool:
    return op_name in should_use_int_tensor_ops_


def should_use_complex_tensor(op_name: str) -> bool:
    return op_name in should_use_complex_tensor_ops_


test_tensor_dim_ops_1_ = frozenset(
    (
        "addmv",
        "index_add",
        "_convert_indices_from_coo_to_csr",
        "_convert_indices_from_csr_to_coo",
        "nll_loss_backward",
        "dot",
        "vdot",
        "outer",
        "ger",
    )
)
test_tensor_dim_ops_2_ = frozenset(
    ("addmm", "mm", "nuclear_norm", "diag", "_addmm_activation", "matrix_H", "t")
)


def test_tensor_dim(op_name: str) -> int:
    if op_name in test_tensor_dim_ops_1_:
        return 1
    if op_name in test_tensor_dim_ops_2_:
        return 2
    return 3


test_tensor_shapes_string = '{"view_as_complex": "{2, 2}"}'
test_tensor_shape_json = json.loads(test_tensor_shapes_string)


def test_tensor_shape(op_name: str) -> str:
    if op_name in test_tensor_shape_json:
        return test_tensor_shape_json[op_name]
    else:
        return ""


def test_value_expression(
    arg_type: Union[BaseType, OptionalType, Type], index: int, op_name: str
) -> str:
    tensor_size_ex = test_tensor_shape(op_name)
    if tensor_size_ex == "":
        num_tensors = 16 if index == 0 else 64
        num_dim = test_tensor_dim(op_name)
        size_per_dim = math.ceil(num_tensors / float(num_dim))
        size_per_dim += size_per_dim % 2
        tensor_size_ex = "{%s}" % (",".join([f"{size_per_dim}"] * num_dim))
    if should_use_int_tensor(op_name):
        tensor_expression = f"at::randint(1, 100, {tensor_size_ex}, at::kInt)"
    elif should_use_complex_tensor(op_name):
        tensor_expression = f"at::randn({tensor_size_ex}, at::kComplexFloat)"
    else:
        tensor_expression = f"at::rand({tensor_size_ex})"

    value_expressions = {
        BaseTy.Tensor: tensor_expression,
        BaseTy.int: "1",
        BaseTy.bool: "false",
        BaseTy.Scalar: "2",
        BaseTy.ScalarType: "at::ScalarType::Float",
        BaseTy.str: '"floor"',
    }

    base_ty_object = None
    if isinstance(arg_type, BaseType):
        base_ty_object = arg_type.name
    else:
        assert isinstance(arg_type, OptionalType) and isinstance(
            arg_type.elem, BaseType
        )
        base_ty_object = arg_type.elem.name
    assert base_ty_object in value_expressions, "not expected type"
    value_expression = value_expressions[base_ty_object]
    return value_expression


def generate_test_value_definitions(schema: FunctionSchema, index: int) -> str:
    assert not schema.is_out_fn()
    schema_name = schema.name.name.base
    arg_map = {}
    for arg in schema.schema_order_arguments():
        test_value_exp = test_value_expression(arg.type, index, schema_name)
        arg_map[arg.name] = test_value_exp
    config.override_test_values(arg_map, schema_name, index)
    arg_populations = []
    for arg_name, arg_value in arg_map.items():
        arg_populations.append(f"auto {arg_name}{index} = {arg_value}")
    return ";\n    ".join(arg_populations) + ";"


def generate_test_value_names(schema: FunctionSchema, index: int) -> str:
    assert not schema.is_out_fn()
    return ",".join(f"{arg.name}{index}" for arg in schema.schema_order_arguments())


generate_test_ir_arguments_base_ty_to_type_str_ = {
    BaseTy.Tensor: "Tensor",
    BaseTy.int: "int",
    BaseTy.float: "float",
    BaseTy.str: "str",
    BaseTy.Scalar: "int",
    BaseTy.ScalarType: "int",
    BaseTy.bool: "bool",
}


def generate_test_ir_arguments(
    schema: FunctionSchema,
) -> List[Tuple[str, Optional[str]]]:
    def ir_argument(arg: Argument) -> Tuple[str, Optional[str]]:
        t = arg.type
        add_optional = False
        if isinstance(t, OptionalType):
            t = t.elem
            add_optional = True
        assert isinstance(t, BaseType)
        type_str = None
        if t.name in generate_test_ir_arguments_base_ty_to_type_str_:
            type_str = generate_test_ir_arguments_base_ty_to_type_str_[t.name]
        if type_str and add_optional:
            type_str = f"{type_str}?"
        return ("%" + arg.name, type_str)

    return [ir_argument(arg) for arg in schema.schema_order_arguments()]


def generate_arg_extraction(schema: FunctionSchema) -> str:
    arg_populations = []
    for i, arg in enumerate(schema.schema_order_arguments()):
        maybe_method = ivalue_type_conversion_method(arg.type)
        assert maybe_method
        is_reference, type_conversion_method = maybe_method
        reference = "&" if is_reference else ""
        arg_populations.append(
            f"const auto{reference} {arg.name} = p_node->Input({i}).{type_conversion_method}"
        )
    return ";\n    ".join(arg_populations) + ";"


def get_kernel_name(g: NativeFunctionsGroup, backend_index: BackendIndex) -> str:
    kernel = backend_index.get_kernel(g.functional)
    if g.structured or kernel is None:
        return cpp.name(g.functional.func)
    return kernel.kernel


def get_out_kernel_name(g: NativeFunctionsGroup, backend_index: BackendIndex) -> str:
    kernel = backend_index.get_kernel(g.out)
    if g.structured or kernel is None:
        return cpp.name(g.out.func)
    return kernel.kernel


def generate_non_out_variant_call(
    g: NativeFunctionsGroup, backend_index: BackendIndex
) -> str:
    schema = g.functional.func
    assert not schema.is_out_fn()
    kernel_name = get_kernel_name(g, backend_index)
    arg_names = (arg.name for arg in schema.schema_order_arguments())
    namespace_name = "cpu" if g.structured else "native"
    return f'at::{namespace_name}::{kernel_name}({",".join(arg_names)})'


def generate_call_to_view_ops(
    g: NativeFunctionsViewGroup, backend_index: BackendIndex
) -> str:
    schema = g.view.func
    kernel_name = cpp.name(schema)
    kernel = backend_index.get_kernel(g.view)
    if kernel:
        kernel_name = kernel.kernel
    arg_names = (arg.name for arg in schema.schema_order_arguments())
    namespace_name = "native"
    return f'at::{namespace_name}::{kernel_name}({",".join(arg_names)})'


def generate_out_variant_call(
    g: NativeFunctionsGroup, backend_index: BackendIndex
) -> str:
    schema = g.out.func
    assert schema.is_out_fn()
    arg_names = []
    kernel_name = get_out_kernel_name(g, backend_index)
    if g.structured:
        # structured op starts with the output tensor argument.
        arg_names = [out_arg.name for out_arg in schema.arguments.out]
    else:
        arg_names = []
    for arg in schema.arguments.non_out:
        if isinstance(arg, SelfArgument):
            arg_names.append(arg.argument.name)
        else:
            assert isinstance(arg, Argument)
            arg_names.append(arg.name)
    if not g.structured:
        assert len(schema.arguments.out) == 1
        arg_names.append(schema.arguments.out[0].name)
    cpp_arg_names = ",".join(arg_names)
    namespace_name = "cpu" if g.structured else "native"
    return f"at::{namespace_name}::{kernel_name}({cpp_arg_names})"


no_memory_resize_ops = frozenset(
    (
        "isin.Scalar_Tensor",
        "index_add",
        "dot",
        "vdot",
        "nuclear_norm",
        "histc",
        "l1_loss",
        "multi_margin_loss",
        "multilabel_margin_loss",
        "nll_loss",
        "nll_loss2d",
    )
)


def should_check_resize(schema: FunctionSchema) -> bool:
    schema_str = str(schema)
    type_variant_op_name = schema_str[: schema_str.find("(")]
    return type_variant_op_name not in no_memory_resize_ops


def op_name_from_group(g: NativeFunctionsGroup) -> str:
    return g.functional.func.name.name.base


class GenOpDispatcher:
    def out_variant(
        self, groups: Sequence[NativeFunctionsGroup], backend_index: BackendIndex
    ) -> str:
        if not groups:
            return ""
        generated_type_variants = []
        for g in groups:
            with native_function_manager(g):
                assert is_supported(g)
                assert isinstance(g, NativeFunctionsGroup)
                generated_type_variant = self.out_variant_op_generator(g, backend_index)
                generated_type_variants.append(generated_type_variant)
        op_name = op_name_from_group(groups[0])
        body = "\n".join(generated_type_variants)
        generated = f"""
REGISTER_OPERATOR_FUNCTOR(
    aten::{op_name},
    aten_{op_name},
    [](Node* n) -> SROperator {{
      {body}
      LogAndDumpSchema(n);
      return nullptr;
    }});
"""
        return generated

    def view(
        self, groups: Sequence[NativeFunctionsViewGroup], backend_index: BackendIndex
    ) -> str:
        if not groups:
            return ""
        generated_type_variants = []
        for g in groups:
            with native_function_manager(g):
                assert is_supported(g)
                assert isinstance(g, NativeFunctionsViewGroup)
                generated_type_variant = self.view_op_generator(g, backend_index)
                generated_type_variants.append(generated_type_variant)
        op_name = config.func_name_base_str(groups[0])
        body = "\n".join(generated_type_variants)
        generated = f"""
REGISTER_NATIVE_OPERATOR_FUNCTOR(
    aten::{op_name},
    aten_{op_name},
    [](Node* n) -> SROperator {{
      {body}
      LogAndDumpSchema(n);
      return nullptr;
    }});
"""
        return generated

    def out_variant_op_generator(
        self, g: NativeFunctionsGroup, backend_index: BackendIndex
    ) -> str:
        functional = g.functional
        schema = str(functional.func)
        populated_argument = generate_arg_extraction(g.functional.func)
        functional_variant_call = generate_non_out_variant_call(g, backend_index)
        assert len(g.out.func.arguments.out) == 1
        out_variable_name = str(g.out.func.arguments.out[0].name)
        out_variant_call = generate_out_variant_call(g, backend_index)
        generated = f"""
      if (n->matches(torch::schema("aten::{schema}"))) {{
        return [](ProcessedNode* p_node) {{
          {populated_argument}
          if (p_node->Output(0).isNone()) {{
            p_node->Output(0) = {functional_variant_call};
            return;
          }}
          auto& {out_variable_name} = p_node->Output(0).toTensor();
          fastResizeToZero({out_variable_name});
          {out_variant_call};
        }};
      }}"""
        return generated

    def view_op_generator(
        self, g: NativeFunctionsViewGroup, backend_index: BackendIndex
    ) -> str:
        schema = str(g.view.func)
        populated_argument = generate_arg_extraction(g.view.func)
        functional_variant_call = generate_call_to_view_ops(g, backend_index)
        generated = f"""
      if (n->matches(torch::schema("aten::{schema}"))) {{
        return [](ProcessedNode* p_node) {{
          {populated_argument}
            p_node->Output(0) = {functional_variant_call};
        }};
      }}"""
        return generated


class GenOpTestCase:
    def out_variant(self, groups: Sequence[NativeFunctionsGroup]) -> str:
        if not groups:
            return ""
        generated_type_variants = []
        for g in groups:
            with native_function_manager(g):
                assert is_supported(g)
                assert isinstance(g, NativeFunctionsGroup)
                generated_type_variant = self.out_variant_op_test_case_generator(g)
                generated_type_variants.append(generated_type_variant)
        return "\n".join(generated_type_variants)

    def view(self, groups: Sequence[NativeFunctionsViewGroup]) -> str:
        if not groups:
            return ""
        generated_type_variants = []
        for g in groups:
            with native_function_manager(g):
                assert is_supported(g)
                assert isinstance(g, NativeFunctionsViewGroup)
                generated_type_variant = self.view_op_test_case_generator(g)
                generated_type_variants.append(generated_type_variant)
        return "\n".join(generated_type_variants)

    def out_variant_op_test_case_generator(self, g: NativeFunctionsGroup) -> str:
        schema = g.functional.func
        schema_str = str(schema)
        assert schema_str.find("(") > 0
        type_variant_op_name = schema_str[: schema_str.find("(")].replace(".", "_")
        op_name = op_name_from_group(g)
        assert type_variant_op_name.startswith(op_name)

        arg_types = generate_test_ir_arguments(schema)
        arg_declarations = ", ".join(
            (
                arg_name if arg_type is None else f"{arg_name}: {arg_type}"
                for arg_name, arg_type in arg_types
            )
        )
        arg_names = ", ".join((arg_name for arg_name, _ in arg_types))
        assert (
            len(schema.returns) == 1
            and isinstance(schema.returns[0].type, BaseType)
            and schema.returns[0].type.name is BaseTy.Tensor
        )
        test_value_definitions = generate_test_value_definitions(schema, 0)
        test_value_names = generate_test_value_names(schema, 0)
        test_value_definitions2 = generate_test_value_definitions(schema, 1)
        test_value_names2 = generate_test_value_names(schema, 1)
        check_resize = "true" if should_check_resize(schema) else "false"
        generated = f"""
TEST(StaticRuntime, autogen_{type_variant_op_name}) {{
  const std::string script = R"IR(
    graph({arg_declarations}):
        %bias: None = prim::Constant()
        %ret = aten::{op_name}({arg_names})
        %cloned = aten::clone(%ret, %bias)
        return (%cloned)
  )IR";

  {test_value_definitions}
  std::vector<IValue> args{{{test_value_names}}};
  testStaticRuntime(script, args, {{}}, /*use_allclose=*/false, /*use_equalnan=*/false, /*check_resize=*/{check_resize});

  {test_value_definitions2}
  std::vector<IValue> args2{{{test_value_names2}}};
  testStaticRuntime(script, args, args2, /*use_allclose=*/false, /*use_equalnan=*/false, /*check_resize=*/{check_resize});

}}
"""
        return generated

    def view_op_test_case_generator(self, g: NativeFunctionsViewGroup) -> str:
        schema = g.view.func
        schema_str = str(schema)
        assert schema_str.find("(") > 0
        type_variant_op_name = schema_str[: schema_str.find("(")].replace(".", "_")
        op_name = g.view.root_name
        assert type_variant_op_name.startswith(op_name)

        arg_types = generate_test_ir_arguments(schema)
        arg_declarations = ", ".join(
            (
                arg_name if arg_type is None else f"{arg_name}: {arg_type}"
                for arg_name, arg_type in arg_types
            )
        )
        arg_names = ", ".join((arg_name for arg_name, _ in arg_types))
        assert (
            len(schema.returns) == 1
            and isinstance(schema.returns[0].type, BaseType)
            and schema.returns[0].type.name is BaseTy.Tensor
        )
        test_value_definitions = generate_test_value_definitions(schema, 0)
        test_value_names = generate_test_value_names(schema, 0)
        generated = f"""
TEST(StaticRuntime, autogen_{type_variant_op_name}) {{
  const std::string script = R"IR(
    graph({arg_declarations}):
        %bias: None = prim::Constant()
        %ret = aten::{op_name}({arg_names})
        %cloned = aten::clone(%ret, %bias)
        return (%cloned)
  )IR";

  {test_value_definitions}
  std::vector<IValue> args{{{test_value_names}}};
  testStaticRuntime(script, args);
}}
"""

        return generated
