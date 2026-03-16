import ast
import colorsys
import math
import os
import random
import re
import shutil
import sqlite3
import sys
import time
import warnings
from collections import Counter, defaultdict, namedtuple
from difflib import SequenceMatcher
from importlib import resources
from pathlib import Path

from diskcache import Cache
from grep_ast import TreeContext, filename_to_lang
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from tqdm import tqdm
from tree_sitter import Query

from aider.dump import dump
from aider.special import filter_important_files
from aider.waiting import Spinner

# tree_sitter is throwing a FutureWarning
warnings.simplefilter("ignore", category=FutureWarning)
from grep_ast.tsl import USING_TSL_PACK, get_language, get_parser  # noqa: E402

Tag = namedtuple("Tag", "rel_fname fname line name kind".split())
FunctionInfo = namedtuple(
    "FunctionInfo",
    "rel_fname fname line name qualname arg_names arg_attribute_names call_names call_names_using_args return_names return_call_names body_tokens has_type_hints".split(),
)
ClassInfo = namedtuple(
    "ClassInfo",
    "rel_fname fname line name qualname method_names attr_names member_names attr_summaries method_summaries".split(),
)
ImportInfo = namedtuple(
    "ImportInfo",
    "rel_fname fname module imported_name local_name full_name kind line".split(),
)


SQLITE_ERRORS = (sqlite3.OperationalError, sqlite3.DatabaseError, OSError)


CACHE_VERSION = 3
if USING_TSL_PACK:
    CACHE_VERSION = 4

UPDATING_REPO_MAP_MESSAGE = "Updating type repo map"


class TypeRepoMap:
    TAGS_CACHE_DIR = f".aider.tags.cache.v{CACHE_VERSION}"

    warned_files = set()

    def __init__(
        self,
        map_tokens=1024,
        root=None,
        main_model=None,
        io=None,
        repo_content_prefix=None,
        verbose=False,
        max_context_window=None,
        map_mul_no_files=8,
        refresh="auto",
    ):
        self.io = io
        self.verbose = verbose
        self.refresh = refresh

        if not root:
            root = os.getcwd()
        self.root = root

        self.load_tags_cache()
        self.cache_threshold = 0.95

        self.max_map_tokens = map_tokens
        self.map_mul_no_files = map_mul_no_files
        self.max_context_window = max_context_window

        self.repo_content_prefix = repo_content_prefix

        self.main_model = main_model

        self.tree_cache = {}
        self.tree_context_cache = {}
        self.map_cache = {}
        self.python_file_cache = {}
        self.map_processing_time = 0
        self.last_map = None

        if self.verbose:
            self.io.tool_output(
                f"RepoMap initialized with map_mul_no_files: {self.map_mul_no_files}"
            )

    def token_count(self, text):
        len_text = len(text)
        if len_text < 200:
            return self.main_model.token_count(text)

        lines = text.splitlines(keepends=True)
        num_lines = len(lines)
        step = num_lines // 100 or 1
        lines = lines[::step]
        sample_text = "".join(lines)
        sample_tokens = self.main_model.token_count(sample_text)
        est_tokens = sample_tokens / len(sample_text) * len_text
        return est_tokens

    def get_repo_map(
        self,
        chat_files,
        other_files,
        mentioned_fnames=None,
        mentioned_idents=None,
        mentioned_target_functions=None,
        force_refresh=False,
    ):
        if self.max_map_tokens <= 0:
            return
        if not other_files:
            return
        if not mentioned_fnames:
            mentioned_fnames = set()
        if not mentioned_idents:
            mentioned_idents = set()
        if not mentioned_target_functions:
            mentioned_target_functions = set()

        max_map_tokens = self.max_map_tokens

        # With no files in the chat, give a bigger view of the entire repo
        padding = 4096
        if max_map_tokens and self.max_context_window:
            target = min(
                int(max_map_tokens * self.map_mul_no_files),
                self.max_context_window - padding,
            )
        else:
            target = 0
        if not chat_files and self.max_context_window and target > 0:
            max_map_tokens = target

        try:
            files_listing = self.get_ranked_tags_map(
                chat_files,
                other_files,
                max_map_tokens,
                mentioned_fnames,
                mentioned_idents,
                mentioned_target_functions,
                force_refresh,
            )
        except RecursionError:
            self.io.tool_error("Disabling repo map, git repo too large?")
            self.max_map_tokens = 0
            return

        if not files_listing:
            return

        if self.verbose:
            num_tokens = self.token_count(files_listing)
            self.io.tool_output(f"Repo-map: {num_tokens / 1024:.1f} k-tokens")

        if chat_files:
            other = "other "
        else:
            other = ""

        if self.repo_content_prefix:
            repo_content = self.repo_content_prefix.format(other=other)
        else:
            repo_content = ""

        repo_content += files_listing

        return repo_content

    def get_rel_fname(self, fname):
        try:
            return os.path.relpath(fname, self.root)
        except ValueError:
            # Issue #1288: ValueError: path is on mount 'C:', start on mount 'D:'
            # Just return the full fname.
            return fname

    def tags_cache_error(self, original_error=None):
        """Handle SQLite errors by trying to recreate cache, falling back to dict if needed"""

        if self.verbose and original_error:
            self.io.tool_warning(f"Tags cache error: {str(original_error)}")

        if isinstance(getattr(self, "TAGS_CACHE", None), dict):
            return

        path = Path(self.root) / self.TAGS_CACHE_DIR

        # Try to recreate the cache
        try:
            # Delete existing cache dir
            if path.exists():
                shutil.rmtree(path)

            # Try to create new cache
            new_cache = Cache(path)

            # Test that it works
            test_key = "test"
            new_cache[test_key] = "test"
            _ = new_cache[test_key]
            del new_cache[test_key]

            # If we got here, the new cache works
            self.TAGS_CACHE = new_cache
            return

        except SQLITE_ERRORS as e:
            # If anything goes wrong, warn and fall back to dict
            self.io.tool_warning(
                f"Unable to use tags cache at {path}, falling back to memory cache"
            )
            if self.verbose:
                self.io.tool_warning(f"Cache recreation error: {str(e)}")

        self.TAGS_CACHE = dict()

    def load_tags_cache(self):
        path = Path(self.root) / self.TAGS_CACHE_DIR
        try:
            self.TAGS_CACHE = Cache(path)
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)

    def save_tags_cache(self):
        pass

    def get_mtime(self, fname):
        try:
            return os.path.getmtime(fname)
        except FileNotFoundError:
            self.io.tool_warning(f"File not found error: {fname}")

    def get_tags(self, fname, rel_fname):
        # Check if the file is in the cache and if the modification time has not changed
        file_mtime = self.get_mtime(fname)
        if file_mtime is None:
            return []

        cache_key = fname
        try:
            val = self.TAGS_CACHE.get(cache_key)  # Issue #1308
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            val = self.TAGS_CACHE.get(cache_key)

        if val is not None and val.get("mtime") == file_mtime:
            try:
                return self.TAGS_CACHE[cache_key]["data"]
            except SQLITE_ERRORS as e:
                self.tags_cache_error(e)
                return self.TAGS_CACHE[cache_key]["data"]

        # miss!
        data = list(self.get_tags_raw(fname, rel_fname))

        # Update the cache
        try:
            self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}
            self.save_tags_cache()
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}

        return data

    def _run_captures(self, query: Query, node):
        # tree-sitter 0.23.2's python bindings had captures directly on the Query object
        # but 0.24.0 moved it to a separate QueryCursor class. Support both.
        if hasattr(query, "captures"):
            # Old API
            return query.captures(node)

        # New API
        from tree_sitter import QueryCursor

        cursor = QueryCursor(query)
        return cursor.captures(node)

    def get_tags_raw(self, fname, rel_fname):
        lang = filename_to_lang(fname)
        if not lang:
            return

        try:
            language = get_language(lang)
            parser = get_parser(lang)
        except Exception as err:
            print(f"Skipping file {fname}: {err}")
            return

        query_scm = get_scm_fname(lang)
        if not query_scm.exists():
            return
        query_scm = query_scm.read_text()

        code = self.io.read_text(fname)
        if not code:
            return
        tree = parser.parse(bytes(code, "utf-8"))

        # Run the tags queries
        captures = self._run_captures(Query(language, query_scm), tree.root_node)

        captures_by_tag = defaultdict(list)
        matches = []
        for tag, nodes in captures.items():
            for node in nodes:
                captures_by_tag[tag].append(node)
            captures_by_tag[tag].append(node)
            matches.append((node, tag))

        if USING_TSL_PACK:
            all_nodes = [(node, tag) for tag, nodes in captures_by_tag.items() for node in nodes]
        else:
            all_nodes = matches

        saw = set()
        for node, tag in all_nodes:
            if tag.startswith("name.definition."):
                kind = "def"
            elif tag.startswith("name.reference."):
                kind = "ref"
            else:
                continue

            # print("GO", node.text, node.start_point[0], tag, rel_fname, fname)

            saw.add(kind)

            result = Tag(
                rel_fname=rel_fname,
                fname=fname,
                name=node.text.decode("utf-8"),
                kind=kind,
                line=node.start_point[0],
            )

            yield result

        if "ref" in saw:
            return
        if "def" not in saw:
            return

        # We saw defs, without any refs
        # Some tags files only provide defs (cpp, for example)
        # Use pygments to backfill refs

        try:
            lexer = guess_lexer_for_filename(fname, code)
        except Exception:  # On Windows, bad ref to time.clock which is deprecated?
            # self.io.tool_error(f"Error lexing {fname}")
            return

        tokens = list(lexer.get_tokens(code))
        tokens = [token[1] for token in tokens if token[0] in Token.Name]

        for token in tokens:
            yield Tag(
                rel_fname=rel_fname,
                fname=fname,
                name=token,
                kind="ref",
                line=-1,
            )

    def _get_python_file_symbols(self, fname, rel_fname):
        if filename_to_lang(fname) != "python":
            return {"functions": [], "classes": [], "imports": []}

        mtime = self.get_mtime(fname)
        key = (fname, mtime)
        if key in self.python_file_cache:
            return self.python_file_cache[key]

        code = self.io.read_text(fname)
        if not code:
            self.python_file_cache[key] = {"functions": [], "classes": [], "imports": []}
            return self.python_file_cache[key]

        try:
            tree = ast.parse(code)
        except SyntaxError:
            self.python_file_cache[key] = {"functions": [], "classes": [], "imports": []}
            return self.python_file_cache[key]

        function_infos = []
        class_infos = []
        import_infos = []

        def flatten_name(node):
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                base = flatten_name(node.value)
                if base:
                    return f"{base}.{node.attr}"
                return node.attr
            return None

        def root_name(node):
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                return root_name(node.value)
            return None

        def split_identifier_tokens(value):
            if not value:
                return set()
            parts = []
            for chunk in value.replace("-", "_").split("."):
                parts.extend(chunk.split("_"))
            tokens = set()
            for part in parts:
                if not part:
                    continue
                part_tokens = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", part)
                if part_tokens:
                    tokens.update(token.lower() for token in part_tokens if token)
                else:
                    tokens.add(part.lower())
            return tokens

        def module_name_candidates_for_rel(rel_path):
            rel_path = rel_path.replace("\\", "/")
            if rel_path.endswith("/__init__.py"):
                module = rel_path[: -len("/__init__.py")].replace("/", ".")
                return {module} if module else set()
            if rel_path.endswith(".py"):
                module = rel_path[:-3].replace("/", ".")
                return {module} if module else set()
            return set()

        current_modules = module_name_candidates_for_rel(rel_fname)

        def resolve_relative_module(module, level):
            if level == 0:
                return module

            bases = sorted(current_modules)
            if not bases:
                return module

            base = bases[0]
            parts = base.split(".") if base else []
            drop = max(level - 1, 0)
            if drop >= len(parts):
                resolved_parts = []
            else:
                resolved_parts = parts[:-drop] if drop else parts

            if module:
                resolved_parts = resolved_parts + module.split(".")

            return ".".join(part for part in resolved_parts if part)

        def safe_unparse(node):
            if node is None:
                return None
            try:
                return ast.unparse(node)
            except Exception:
                return None

        def infer_value_type(node):
            if node is None:
                return None
            if isinstance(node, ast.Constant):
                value = node.value
                if value is None:
                    return "None"
                if isinstance(value, bool):
                    return "bool"
                if isinstance(value, int):
                    return "int"
                if isinstance(value, float):
                    return "float"
                if isinstance(value, str):
                    return "str"
                if isinstance(value, bytes):
                    return "bytes"
            if isinstance(node, ast.List):
                return "list"
            if isinstance(node, ast.Dict):
                return "dict"
            if isinstance(node, ast.Set):
                return "set"
            if isinstance(node, ast.Tuple):
                return "tuple"
            if isinstance(node, ast.Call):
                return safe_unparse(node.func)
            return None

        def format_arg(arg, default=None):
            parts = [arg.arg]
            annotation = safe_unparse(arg.annotation)
            if annotation:
                parts.append(f": {annotation}")
            if default is not None:
                default_text = safe_unparse(default)
                if default_text:
                    parts.append(f" = {default_text}")
            return "".join(parts)

        def format_function_signature(node):
            args = []
            posonly = list(node.args.posonlyargs)
            regular = list(node.args.args)
            kwonly = list(node.args.kwonlyargs)
            defaults = list(node.args.defaults)
            positional = posonly + regular
            num_defaults = len(defaults)
            default_offset = len(positional) - num_defaults

            for index, arg in enumerate(positional):
                default = None
                if index >= default_offset and num_defaults:
                    default = defaults[index - default_offset]
                args.append(format_arg(arg, default))

            if posonly:
                args.insert(len(posonly), "/")

            if node.args.vararg:
                args.append("*" + format_arg(node.args.vararg))
            elif kwonly:
                args.append("*")

            for arg, default in zip(kwonly, node.args.kw_defaults):
                args.append(format_arg(arg, default))

            if node.args.kwarg:
                args.append("**" + format_arg(node.args.kwarg))

            signature = f"def {node.name}({', '.join(args)})"
            return_annotation = safe_unparse(node.returns)
            if return_annotation:
                signature += f" -> {return_annotation}"
            return signature

        def collect_expr_names(node):
            names = set()

            class ExprVisitor(ast.NodeVisitor):
                def visit_Name(self, inner_node):
                    names.add(inner_node.id)

                def visit_Attribute(self, inner_node):
                    full_name = flatten_name(inner_node)
                    if full_name:
                        names.add(full_name)
                    names.add(inner_node.attr)
                    self.generic_visit(inner_node)

                def visit_FunctionDef(self, inner_node):
                    return

                def visit_AsyncFunctionDef(self, inner_node):
                    return

                def visit_ClassDef(self, inner_node):
                    return

            if node is not None:
                ExprVisitor().visit(node)

            normalized = set()
            for name in names:
                normalized.add(name)
                normalized.add(name.split(".")[-1])
            return normalized

        class BodyVisitor(ast.NodeVisitor):
            def __init__(self, arg_names):
                self.arg_names = set(arg_names)
                self.arg_attribute_names = set()
                self.call_names = set()
                self.call_names_using_args = set()
                self.return_names = set()
                self.return_call_names = set()
                self.body_tokens = set(arg.lower() for arg in arg_names)

            def visit_Call(self, node):
                call_name = flatten_name(node.func)
                if call_name:
                    self.call_names.add(call_name)
                    self.call_names.add(call_name.split(".")[-1])
                    self.body_tokens.update(split_identifier_tokens(call_name))

                expr_names = collect_expr_names(node)
                if call_name and self.arg_names.intersection(expr_names):
                    self.call_names_using_args.add(call_name)
                    self.call_names_using_args.add(call_name.split(".")[-1])

                self.generic_visit(node)

            def visit_Return(self, node):
                expr_names = collect_expr_names(node.value)
                self.return_names.update(expr_names)
                for name in expr_names:
                    self.body_tokens.update(split_identifier_tokens(name))

                if node.value is not None:
                    for child in ast.walk(node.value):
                        if isinstance(child, ast.Call):
                            call_name = flatten_name(child.func)
                            if call_name:
                                self.return_call_names.add(call_name)
                                self.return_call_names.add(call_name.split(".")[-1])

                if node.value is not None:
                    self.generic_visit(node.value)

            def visit_Name(self, node):
                self.body_tokens.update(split_identifier_tokens(node.id))

            def visit_Attribute(self, node):
                full_name = flatten_name(node)
                if full_name:
                    self.body_tokens.update(split_identifier_tokens(full_name))
                self.body_tokens.update(split_identifier_tokens(node.attr))

                owner_name = root_name(node.value)
                if owner_name in self.arg_names:
                    self.arg_attribute_names.add(node.attr)

                self.generic_visit(node)

            def visit_FunctionDef(self, node):
                return

            def visit_AsyncFunctionDef(self, node):
                return

            def visit_ClassDef(self, node):
                return

        class SelfAttributeCollector(ast.NodeVisitor):
            def __init__(self):
                self.attrs = {}

            def visit_Assign(self, node):
                for target in node.targets:
                    self._collect_target(target, infer_value_type(node.value))
                self.generic_visit(node.value)

            def visit_AnnAssign(self, node):
                inferred_type = safe_unparse(node.annotation) or infer_value_type(node.value)
                self._collect_target(node.target, inferred_type)
                if node.value is not None:
                    self.generic_visit(node.value)

            def _record_attr(self, name, attr_type=None):
                existing = self.attrs.get(name)
                if existing:
                    return
                self.attrs[name] = attr_type

            def _collect_target(self, node, attr_type=None):
                if isinstance(node, ast.Attribute) and root_name(node.value) in {"self", "cls"}:
                    self._record_attr(node.attr, attr_type)
                elif isinstance(node, (ast.Tuple, ast.List)):
                    for elt in node.elts:
                        self._collect_target(elt, attr_type)

        def process_function(node, scope):
            arg_names = []
            args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
            for arg in args:
                if arg.arg not in {"self", "cls"}:
                    arg_names.append(arg.arg)
            if node.args.vararg:
                if node.args.vararg.arg not in {"self", "cls"}:
                    arg_names.append(node.args.vararg.arg)
            if node.args.kwarg:
                if node.args.kwarg.arg not in {"self", "cls"}:
                    arg_names.append(node.args.kwarg.arg)

            qual_parts = scope + [node.name]
            qualname = ".".join(qual_parts)

            signature_args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
            signature_args = [arg for arg in signature_args if arg.arg not in {"self", "cls"}]
            has_typed_args = any(arg.annotation is not None for arg in signature_args)
            has_typed_vararg = node.args.vararg is not None and node.args.vararg.annotation is not None
            has_typed_kwarg = node.args.kwarg is not None and node.args.kwarg.annotation is not None
            has_typed_return = node.returns is not None
            has_type_hints = has_typed_args or has_typed_vararg or has_typed_kwarg or has_typed_return

            body_visitor = BodyVisitor(arg_names)
            for child in node.body:
                body_visitor.visit(child)

            function_infos.append(
                FunctionInfo(
                    rel_fname=rel_fname,
                    fname=fname,
                    line=max(getattr(node, "lineno", 1) - 1, 0),
                    name=node.name,
                    qualname=qualname,
                    arg_names=tuple(arg_names),
                    arg_attribute_names=frozenset(body_visitor.arg_attribute_names),
                    call_names=frozenset(body_visitor.call_names),
                    call_names_using_args=frozenset(body_visitor.call_names_using_args),
                    return_names=frozenset(body_visitor.return_names),
                    return_call_names=frozenset(body_visitor.return_call_names),
                    body_tokens=frozenset(body_visitor.body_tokens),
                    has_type_hints=has_type_hints,
                )
            )

            next_scope = scope + [node.name]
            for child in node.body:
                if isinstance(child, ast.ClassDef):
                    process_class(child, next_scope)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    process_function(child, next_scope)

        def process_class(node, scope):
            method_names = set()
            attr_names = set()
            attr_types = {}
            method_summaries = []

            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_names.add(child.name)
                    method_summaries.append(format_function_signature(child))
                    collector = SelfAttributeCollector()
                    for stmt in child.body:
                        collector.visit(stmt)
                    attr_names.update(collector.attrs.keys())
                    for attr_name, attr_type in collector.attrs.items():
                        if attr_name not in attr_types or not attr_types[attr_name]:
                            attr_types[attr_name] = attr_type
                elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                    declared_type = None
                    if isinstance(child, ast.AnnAssign):
                        declared_type = safe_unparse(child.annotation) or infer_value_type(child.value)
                    elif isinstance(child, ast.Assign):
                        declared_type = infer_value_type(child.value)
                    for target in targets:
                        if isinstance(target, ast.Name):
                            attr_names.add(target.id)
                            if target.id not in attr_types or not attr_types[target.id]:
                                attr_types[target.id] = declared_type
                        elif isinstance(target, (ast.Tuple, ast.List)):
                            for elt in target.elts:
                                if isinstance(elt, ast.Name):
                                    attr_names.add(elt.id)
                                    if elt.id not in attr_types or not attr_types[elt.id]:
                                        attr_types[elt.id] = declared_type

            attr_summaries = []
            for attr_name in sorted(attr_names):
                attr_type = attr_types.get(attr_name)
                if attr_type:
                    attr_summaries.append(f"{attr_name}: {attr_type}")
                else:
                    attr_summaries.append(attr_name)

            qualname = ".".join(scope + [node.name])
            class_infos.append(
                ClassInfo(
                    rel_fname=rel_fname,
                    fname=fname,
                    line=max(getattr(node, "lineno", 1) - 1, 0),
                    name=node.name,
                    qualname=qualname,
                    method_names=frozenset(method_names),
                    attr_names=frozenset(attr_names),
                    member_names=frozenset(method_names | attr_names),
                    attr_summaries=tuple(sorted(attr_summaries)),
                    method_summaries=tuple(sorted(method_summaries)),
                )
            )

            next_scope = scope + [node.name]
            for child in node.body:
                if isinstance(child, ast.ClassDef):
                    process_class(child, next_scope)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    process_function(child, next_scope)

        for child in tree.body:
            if isinstance(child, ast.Import):
                for alias in child.names:
                    full_name = alias.name
                    local_name = alias.asname or alias.name.split(".")[0]
                    import_infos.append(
                        ImportInfo(
                            rel_fname=rel_fname,
                            fname=fname,
                            module=alias.name,
                            imported_name=None,
                            local_name=local_name,
                            full_name=full_name,
                            kind="import",
                            line=max(getattr(child, "lineno", 1) - 1, 0),
                        )
                    )
            elif isinstance(child, ast.ImportFrom):
                resolved_module = resolve_relative_module(child.module, child.level)
                for alias in child.names:
                    imported_name = alias.name
                    local_name = alias.asname or alias.name
                    full_name = f"{resolved_module}.{imported_name}" if resolved_module else imported_name
                    import_infos.append(
                        ImportInfo(
                            rel_fname=rel_fname,
                            fname=fname,
                            module=resolved_module,
                            imported_name=imported_name,
                            local_name=local_name,
                            full_name=full_name,
                            kind="from",
                            line=max(getattr(child, "lineno", 1) - 1, 0),
                        )
                    )

        for child in tree.body:
            if isinstance(child, ast.ClassDef):
                process_class(child, [])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                process_function(child, [])

        self.python_file_cache[key] = {
            "functions": function_infos,
            "classes": class_infos,
            "imports": import_infos,
        }
        return self.python_file_cache[key]

    def _get_python_file_info(self, fname, rel_fname):
        return self._get_python_file_symbols(fname, rel_fname)["functions"]

    def _get_python_class_info(self, fname, rel_fname):
        return self._get_python_file_symbols(fname, rel_fname)["classes"]

    def _get_python_import_info(self, fname, rel_fname):
        return self._get_python_file_symbols(fname, rel_fname)["imports"]

    def _module_name_candidates(self, rel_fname):
        rel_fname = rel_fname.replace("\\", "/")
        if rel_fname.endswith("/__init__.py"):
            module = rel_fname[: -len("/__init__.py")].replace("/", ".")
            return {module} if module else set()
        if rel_fname.endswith(".py"):
            module = rel_fname[:-3].replace("/", ".")
            return {module} if module else set()
        return set()

    def _qualname_candidates(self, rel_fname, qualname):
        candidates = set()
        if qualname:
            candidates.add(qualname)
            candidates.add(qualname.split(".")[-1])
        for module_name in self._module_name_candidates(rel_fname):
            if qualname:
                candidates.add(f"{module_name}.{qualname}")
                candidates.add(f"{module_name}.{qualname.split('.')[-1]}")
            else:
                candidates.add(module_name)
        return {candidate for candidate in candidates if candidate}

    def _build_type_inference_context(self, fnames):
        function_infos = []
        class_infos = []
        import_infos_by_file = defaultdict(list)
        functions_by_name = defaultdict(list)
        functions_by_qualname = defaultdict(list)
        classes_by_member_name = defaultdict(list)
        classes_by_qualname = defaultdict(list)
        classes_by_file = defaultdict(list)
        modules_to_files = defaultdict(set)
        def_tags_by_name = defaultdict(list)
        def_tags_by_file_name = defaultdict(list)

        for fname in sorted(set(fnames)):
            rel_fname = self.get_rel_fname(fname)

            tags = list(self.get_tags(fname, rel_fname) or [])
            for tag in tags:
                if tag.kind == "def":
                    def_tags_by_name[tag.name].append(tag)
                    def_tags_by_file_name[(tag.rel_fname, tag.name)].append(tag)

            for module_name in self._module_name_candidates(rel_fname):
                modules_to_files[module_name].add(rel_fname)

            for info in self._get_python_file_info(fname, rel_fname):
                function_infos.append(info)
                functions_by_name[info.name].append(info)
                for candidate in self._qualname_candidates(info.rel_fname, info.qualname):
                    functions_by_qualname[candidate].append(info)
                if "." in info.qualname:
                    functions_by_qualname[".".join(info.qualname.split(".")[-2:])].append(info)

            for info in self._get_python_class_info(fname, rel_fname):
                class_infos.append(info)
                classes_by_file[info.rel_fname].append(info)
                for member_name in info.member_names:
                    classes_by_member_name[member_name].append(info)
                for candidate in self._qualname_candidates(info.rel_fname, info.qualname):
                    classes_by_qualname[candidate].append(info)

            import_infos_by_file[rel_fname].extend(self._get_python_import_info(fname, rel_fname))

        return (
            function_infos,
            class_infos,
            import_infos_by_file,
            functions_by_name,
            functions_by_qualname,
            classes_by_member_name,
            classes_by_qualname,
            classes_by_file,
            modules_to_files,
            def_tags_by_name,
            def_tags_by_file_name,
        )

    def _find_target_functions(self, mentioned_target_functions, chat_fnames, all_function_infos):
        if not mentioned_target_functions:
            return []

        chat_rel_fnames = {self.get_rel_fname(fname) for fname in chat_fnames}

        targets = []
        seen = set()

        for target_name in mentioned_target_functions:
            exact_matches = []
            suffix_matches = []
            for info in all_function_infos:
                if info.rel_fname not in chat_rel_fnames:
                    continue
                if info.qualname == target_name or info.name == target_name:
                    exact_matches.append(info)
                elif info.qualname.endswith(f".{target_name}"):
                    suffix_matches.append(info)

            matches = exact_matches or suffix_matches

            for match in matches:
                key = (match.rel_fname, match.qualname, match.line)
                if key not in seen:
                    seen.add(key)
                    targets.append(match)

        return targets

    def _identifier_tokens(self, value):
        if not value:
            return set()

        tokens = set()
        for chunk in str(value).replace("-", "_").split("."):
            for part in chunk.split("_"):
                if not part:
                    continue
                extracted = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", part)
                if extracted:
                    tokens.update(token.lower() for token in extracted if token)
                else:
                    tokens.add(part.lower())
        return tokens

    def _jaccard(self, left, right):
        left = set(left)
        right = set(right)
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    def _function_similarity(self, target_info, candidate_info):
        target_name = target_info.name.lower()
        candidate_name = candidate_info.name.lower()
        target_qualname = target_info.qualname.lower()
        candidate_qualname = candidate_info.qualname.lower()

        name_ratio = max(
            SequenceMatcher(None, target_name, candidate_name).ratio(),
            SequenceMatcher(None, target_qualname, candidate_qualname).ratio(),
        )
        name_token_overlap = self._jaccard(
            self._identifier_tokens(target_info.qualname),
            self._identifier_tokens(candidate_info.qualname),
        )
        arg_overlap = self._jaccard(
            [arg.lower() for arg in target_info.arg_names],
            [arg.lower() for arg in candidate_info.arg_names],
        )
        body_overlap = self._jaccard(target_info.body_tokens, candidate_info.body_tokens)

        score = name_ratio * 0.45 + name_token_overlap * 0.2 + arg_overlap * 0.25 + body_overlap * 0.1

        if target_info.name == candidate_info.name:
            score += 0.2
        if set(arg.lower() for arg in target_info.arg_names) & set(
            arg.lower() for arg in candidate_info.arg_names
        ):
            score += 0.1

        return min(score, 1.0)

    def _match_function_infos(self, symbol, functions_by_name, functions_by_qualname):
        matches = []
        seen = set()

        candidates = [symbol]
        if symbol and "." in symbol:
            candidates.append(symbol.split(".")[-1])
            candidates.append(".".join(symbol.split(".")[-2:]))

        for candidate in candidates:
            for info in functions_by_qualname.get(candidate, []):
                key = (info.rel_fname, info.qualname, info.line)
                if key not in seen:
                    seen.add(key)
                    matches.append(info)
            for info in functions_by_name.get(candidate, []):
                key = (info.rel_fname, info.qualname, info.line)
                if key not in seen:
                    seen.add(key)
                    matches.append(info)

        return matches

    def _tag_key(self, tag):
        if type(tag) is Tag:
            return (tag.rel_fname, tag.name, tag.line, tag.kind)
        return (tag[0], None, None, None)

    def _rank_sort_key(self, item):
        tag = item["tag"]
        if type(tag) is Tag:
            return (-item["score"], tag.rel_fname, tag.line, tag.name)
        return (-item["score"], tag[0], -1, "")

    def _add_scored_item(self, scored_items, tag, score):
        if score <= 0:
            return
        key = self._tag_key(tag)
        if key in scored_items:
            scored_items[key]["score"] += score
        else:
            scored_items[key] = {"tag": tag, "score": score}

    def _get_best_definition_tag(self, rel_fname, name, def_tags_by_file_name):
        tags = def_tags_by_file_name.get((rel_fname, name), [])
        if tags:
            return min(tags, key=lambda tag: tag.line)
        return None

    def _get_import_ranked_tags(
        self,
        target_functions,
        import_infos_by_file,
        classes_by_qualname,
        classes_by_file,
        modules_to_files,
        def_tags_by_file_name,
        chat_rel_fnames,
    ):
        scored_items = {}

        for target_info in target_functions:
            for import_info in import_infos_by_file.get(target_info.rel_fname, []):
                local_name = import_info.local_name
                full_name = import_info.full_name

                for candidate_key in [local_name, full_name, import_info.imported_name, import_info.module]:
                    if not candidate_key:
                        continue

                    for class_info in classes_by_qualname.get(candidate_key, []):
                        if class_info.rel_fname in chat_rel_fnames:
                            continue
                        class_tag = self._get_best_definition_tag(
                            class_info.rel_fname,
                            class_info.name,
                            def_tags_by_file_name,
                        )

                        if class_tag is None:
                            class_tag = Tag(
                                rel_fname=class_info.rel_fname,
                                fname=class_info.fname,
                                line=class_info.line,
                                name=class_info.name,
                                kind="def",
                            )
                        self._add_scored_item(scored_items, class_tag, 105)

                for module_name in [import_info.module, full_name]:
                    if not module_name:
                        continue
                    for rel_fname in modules_to_files.get(module_name, set()):
                        if rel_fname in chat_rel_fnames:
                            continue

                        for class_info in classes_by_file.get(rel_fname, []):
                            class_tag = self._get_best_definition_tag(
                                class_info.rel_fname,
                                class_info.name,
                                def_tags_by_file_name,
                            )
                            if class_tag is None:
                                class_tag = Tag(
                                    rel_fname=class_info.rel_fname,
                                    fname=class_info.fname,
                                    line=class_info.line,
                                    name=class_info.name,
                                    kind="def",
                                )
                            self._add_scored_item(scored_items, class_tag, 95)

                        # self._add_scored_item(scored_items, (rel_fname,), 55)

        ranked = sorted(
            (value for value in scored_items.values() if value["score"] > 0),
            key=self._rank_sort_key,
        )
        return [item["tag"] for item in ranked]

    def _get_type_inference_ranked_tags(
        self,
        chat_fnames,
        other_fnames,
        mentioned_target_functions,
        progress=None,
    ):

        if not mentioned_target_functions:
            return []

        fnames = set(chat_fnames).union(set(other_fnames))
        chat_rel_fnames = {self.get_rel_fname(fname) for fname in chat_fnames}

        (
            all_function_infos,
            all_class_infos,
            import_infos_by_file,
            functions_by_name,
            functions_by_qualname,
            classes_by_member_name,
            classes_by_qualname,
            classes_by_file,
            modules_to_files,
            def_tags_by_name,
            def_tags_by_file_name,
        ) = self._build_type_inference_context(fnames)

        target_functions = self._find_target_functions(
            mentioned_target_functions,
            chat_fnames,
            all_function_infos,
        )

        # print(f"Found {len(target_functions)} target functions for type inference.")
       
        if not target_functions:
            return []

        scored_items = {}
        import_ranked_tags = self._get_import_ranked_tags(
            target_functions,
            import_infos_by_file,
            classes_by_qualname,
            classes_by_file,
            modules_to_files,
            def_tags_by_file_name,
            chat_rel_fnames,
        )

        # print(f"Import ranked tags: {[tag for tag in import_ranked_tags]}")

        for index, tag in enumerate(import_ranked_tags):
            self._add_scored_item(scored_items, tag, max(40, 90 - index))

        for target_info in target_functions:
            if progress:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {target_info.qualname}")

            for attr_name in target_info.arg_attribute_names:
                for class_info in classes_by_member_name.get(attr_name, []):
                    if class_info.rel_fname in chat_rel_fnames:
                        continue

                    class_tag = self._get_best_definition_tag(
                        class_info.rel_fname,
                        class_info.name,
                        def_tags_by_file_name,
                    )

                    if class_tag is None:
                        class_tag = Tag(
                            rel_fname=class_info.rel_fname,
                            fname=class_info.fname,
                            line=class_info.line,
                            name=class_info.name,
                            kind="def",
                        )
                    self._add_scored_item(scored_items, class_tag, 125)

                    member_tag = self._get_best_definition_tag(
                        class_info.rel_fname,
                        attr_name,
                        def_tags_by_file_name,
                    )
                    if member_tag is not None:
                        self._add_scored_item(scored_items, member_tag, 85)

            for call_name in target_info.call_names_using_args:
                for candidate in self._match_function_infos(
                    call_name,
                    functions_by_name,
                    functions_by_qualname,
                ):
                    if candidate.rel_fname in chat_rel_fnames:
                        continue
                    tag = Tag(
                        rel_fname=candidate.rel_fname,
                        fname=candidate.fname,
                        line=candidate.line,
                        name=candidate.name,
                        kind="def",
                    )
                    self._add_scored_item(scored_items, tag, 140)

            for candidate in all_function_infos:
                if candidate.rel_fname in chat_rel_fnames:
                    continue
                if not candidate.has_type_hints:
                    continue
                if (
                    candidate.rel_fname == target_info.rel_fname
                    and candidate.qualname == target_info.qualname
                    and candidate.line == target_info.line
                ):
                    continue

                similarity = self._function_similarity(target_info, candidate)
                if similarity < 0.7:
                    continue

                tag = Tag(
                    rel_fname=candidate.rel_fname,
                    fname=candidate.fname,
                    line=candidate.line,
                    name=candidate.name,
                    kind="def",
                )
                self._add_scored_item(scored_items, tag, 90 * similarity)

            for return_call_name in target_info.return_call_names:
                for candidate in self._match_function_infos(
                    return_call_name,
                    functions_by_name,
                    functions_by_qualname,
                ):

                    if candidate.rel_fname in chat_rel_fnames:
                        continue
                    tag = Tag(
                        rel_fname=candidate.rel_fname,
                        fname=candidate.fname,
                        line=candidate.line,
                        name=candidate.name,
                        kind="def",
                    )
                    self._add_scored_item(scored_items, tag, 110)

            for return_name in target_info.return_names:
                for name_variant in {return_name, return_name.split(".")[-1]}:
                    for tag in def_tags_by_name.get(name_variant, []):
                        if tag.rel_fname in chat_rel_fnames:
                            continue
                        boost = 70 if name_variant in target_info.return_call_names else 45
                        self._add_scored_item(scored_items, tag, boost)

        ranked = sorted(
            (value for value in scored_items.values() if value["score"] > 0),
            key=self._rank_sort_key,
        )

        # print(ranked)

        return [item["tag"] for item in ranked if isinstance(item["tag"], Tag)]

    def _merge_ranked_tags(self, special_ranked_tags, base_ranked_tags):
        if not special_ranked_tags:
            return base_ranked_tags

        combined = {}

        base_total = max(len(base_ranked_tags), 1)
        for index, tag in enumerate(base_ranked_tags):
            key = self._tag_key(tag)
            combined[key] = {
                "tag": tag,
                "score": combined.get(key, {}).get("score", 0.0)
                + ((base_total - index) / base_total),
            }

        for index, tag in enumerate(special_ranked_tags):
            key = self._tag_key(tag)
            special_bonus = max(5.0, len(special_ranked_tags) - index)
            if key in combined:
                combined[key]["score"] += special_bonus
            else:
                combined[key] = {"tag": tag, "score": special_bonus}

        merged = sorted(
            combined.values(),
            key=self._rank_sort_key,
        )
        return [item["tag"] for item in merged]

    def get_ranked_tags(
        self,
        chat_fnames,
        other_fnames,
        mentioned_fnames,
        mentioned_idents,
        mentioned_target_functions=None,
        progress=None,
    ):
        base_ranked_tags = self._get_original_ranked_tags(
            chat_fnames,
            other_fnames,
            mentioned_fnames,
            mentioned_idents,
            progress=progress,
        )

        special_ranked_tags = self._get_type_inference_ranked_tags(
            chat_fnames,
            other_fnames,
            mentioned_target_functions,
            progress=progress,
        )

        print("Length Versus:", len(base_ranked_tags), len(special_ranked_tags))

        # TODO: We have to make a option for merge strategy here.
        if not special_ranked_tags:
            return []
            # return base_ranked_tags
        else:
            # print("RETURN SPECIAL RANKED TAGS ONLY")
            # print(special_ranked_tags)
            return special_ranked_tags

        # return self._merge_ranked_tags(special_ranked_tags, base_ranked_tags)

    def _get_original_ranked_tags(
        self, chat_fnames, other_fnames, mentioned_fnames, mentioned_idents, progress=None
    ):
        import networkx as nx

        defines = defaultdict(set)
        references = defaultdict(list)
        definitions = defaultdict(set)

        personalization = dict()

        fnames = set(chat_fnames).union(set(other_fnames))

        chat_rel_fnames = set()

        fnames = sorted(fnames)

        # Default personalization for unspecified files is 1/num_nodes
        # https://networkx.org/documentation/stable/_modules/networkx/algorithms/link_analysis/pagerank_alg.html#pagerank
        personalize = 100 / len(fnames)

        try:
            cache_size = len(self.TAGS_CACHE)
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            cache_size = len(self.TAGS_CACHE)

        if len(fnames) - cache_size > 100:
            self.io.tool_output(
                "Initial repo scan can be slow in larger repos, but only happens once."
            )
            fnames = tqdm(fnames, desc="Scanning repo")
            showing_bar = True
        else:
            showing_bar = False

        for fname in fnames:
            if self.verbose:
                self.io.tool_output(f"Processing {fname}")
            if progress and not showing_bar:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {fname}")

            try:
                file_ok = Path(fname).is_file()
            except OSError:
                file_ok = False

            if not file_ok:
                if fname not in self.warned_files:
                    self.io.tool_warning(f"Repo-map can't include {fname}")
                    self.io.tool_output(
                        "Has it been deleted from the file system but not from git?"
                    )
                    self.warned_files.add(fname)
                continue

            # dump(fname)
            rel_fname = self.get_rel_fname(fname)
            current_pers = 0.0  # Start with 0 personalization score

            if fname in chat_fnames:
                current_pers += personalize
                chat_rel_fnames.add(rel_fname)

            if rel_fname in mentioned_fnames:
                # Use max to avoid double counting if in chat_fnames and mentioned_fnames
                current_pers = max(current_pers, personalize)

            # Check path components against mentioned_idents
            path_obj = Path(rel_fname)
            path_components = set(path_obj.parts)
            basename_with_ext = path_obj.name
            basename_without_ext, _ = os.path.splitext(basename_with_ext)
            components_to_check = path_components.union({basename_with_ext, basename_without_ext})

            matched_idents = components_to_check.intersection(mentioned_idents)
            if matched_idents:
                # Add personalization *once* if any path component matches a mentioned ident
                current_pers += personalize

            if current_pers > 0:
                personalization[rel_fname] = current_pers  # Assign the final calculated value

            tags = list(self.get_tags(fname, rel_fname))
            if tags is None:
                continue

            for tag in tags:
                if tag.kind == "def":
                    defines[tag.name].add(rel_fname)
                    key = (rel_fname, tag.name)
                    definitions[key].add(tag)

                elif tag.kind == "ref":
                    references[tag.name].append(rel_fname)

        ##
        # dump(defines)
        # dump(references)
        # dump(personalization)

        if not references:
            references = dict((k, list(v)) for k, v in defines.items())

        idents = set(defines.keys()).intersection(set(references.keys()))

        G = nx.MultiDiGraph()

        # Add a small self-edge for every definition that has no references
        # Helps with tree-sitter 0.23.2 with ruby, where "def greet(name)"
        # isn't counted as a def AND a ref. tree-sitter 0.24.0 does.
        for ident in defines.keys():
            if ident in references:
                continue
            for definer in defines[ident]:
                G.add_edge(definer, definer, weight=0.1, ident=ident)

        for ident in idents:
            if progress:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {ident}")

            definers = defines[ident]

            mul = 1.0

            is_snake = ("_" in ident) and any(c.isalpha() for c in ident)
            is_kebab = ("-" in ident) and any(c.isalpha() for c in ident)
            is_camel = any(c.isupper() for c in ident) and any(c.islower() for c in ident)
            if ident in mentioned_idents:
                mul *= 10
            if (is_snake or is_kebab or is_camel) and len(ident) >= 8:
                mul *= 10
            if ident.startswith("_"):
                mul *= 0.1
            if len(defines[ident]) > 5:
                mul *= 0.1

            for referencer, num_refs in Counter(references[ident]).items():
                for definer in definers:
                    # dump(referencer, definer, num_refs, mul)
                    # if referencer == definer:
                    #    continue

                    use_mul = mul
                    if referencer in chat_rel_fnames:
                        use_mul *= 50

                    # scale down so high freq (low value) mentions don't dominate
                    num_refs = math.sqrt(num_refs)

                    G.add_edge(referencer, definer, weight=use_mul * num_refs, ident=ident)

        if not references:
            pass

        if personalization:
            pers_args = dict(personalization=personalization, dangling=personalization)
        else:
            pers_args = dict()

        try:
            ranked = nx.pagerank(G, weight="weight", **pers_args)
        except ZeroDivisionError:
            # Issue #1536
            try:
                ranked = nx.pagerank(G, weight="weight")
            except ZeroDivisionError:
                return []

        # distribute the rank from each source node, across all of its out edges
        ranked_definitions = defaultdict(float)
        for src in G.nodes:
            if progress:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {src}")

            src_rank = ranked[src]
            total_weight = sum(data["weight"] for _src, _dst, data in G.out_edges(src, data=True))
            # dump(src, src_rank, total_weight)
            for _src, dst, data in G.out_edges(src, data=True):
                data["rank"] = src_rank * data["weight"] / total_weight
                ident = data["ident"]
                ranked_definitions[(dst, ident)] += data["rank"]

        ranked_tags = []
        ranked_definitions = sorted(
            ranked_definitions.items(), reverse=True, key=lambda x: (x[1], x[0])
        )

        # dump(ranked_definitions)

        for (fname, ident), rank in ranked_definitions:
            # print(f"{rank:.03f} {fname} {ident}")
            if fname in chat_rel_fnames:
                continue
            ranked_tags += list(definitions.get((fname, ident), []))

        rel_other_fnames_without_tags = set(self.get_rel_fname(fname) for fname in other_fnames)

        fnames_already_included = set(rt[0] for rt in ranked_tags)

        top_rank = sorted([(rank, node) for (node, rank) in ranked.items()], reverse=True)
        for rank, fname in top_rank:
            if fname in rel_other_fnames_without_tags:
                rel_other_fnames_without_tags.remove(fname)
            if fname not in fnames_already_included:
                ranked_tags.append((fname,))

        for fname in rel_other_fnames_without_tags:
            ranked_tags.append((fname,))

        return ranked_tags

    def get_ranked_tags_map(
        self,
        chat_fnames,
        other_fnames=None,
        max_map_tokens=None,
        mentioned_fnames=None,
        mentioned_idents=None,
        mentioned_target_functions=None,
        force_refresh=False,
    ):
        # Create a cache key
        cache_key = [
            tuple(sorted(chat_fnames)) if chat_fnames else None,
            tuple(sorted(other_fnames)) if other_fnames else None,
            max_map_tokens,
        ]

        if self.refresh == "auto":
            cache_key += [
                tuple(sorted(mentioned_fnames)) if mentioned_fnames else None,
                tuple(sorted(mentioned_idents)) if mentioned_idents else None,
                tuple(sorted(mentioned_target_functions)) if mentioned_target_functions else None,
            ]
        cache_key = tuple(cache_key)

        use_cache = False
        if not force_refresh:
            if self.refresh == "manual" and self.last_map:
                return self.last_map

            if self.refresh == "always":
                use_cache = False
            elif self.refresh == "files":
                use_cache = True
            elif self.refresh == "auto":
                use_cache = self.map_processing_time > 1.0

            # Check if the result is in the cache
            if use_cache and cache_key in self.map_cache:
                return self.map_cache[cache_key]

        # If not in cache or force_refresh is True, generate the map
        start_time = time.time()
        result = self.get_ranked_tags_map_uncached(
            chat_fnames,
            other_fnames,
            max_map_tokens,
            mentioned_fnames,
            mentioned_idents,
            mentioned_target_functions,
        )
        end_time = time.time()
        self.map_processing_time = end_time - start_time

        # Store the result in the cache
        self.map_cache[cache_key] = result
        self.last_map = result

        return result

    def get_ranked_tags_map_uncached(
        self,
        chat_fnames,
        other_fnames=None,
        max_map_tokens=None,
        mentioned_fnames=None,
        mentioned_idents=None,
        mentioned_target_functions=None,
    ):
        if not other_fnames:
            other_fnames = list()
        if not max_map_tokens:
            max_map_tokens = self.max_map_tokens
        if not mentioned_fnames:
            mentioned_fnames = set()
        if not mentioned_idents:
            mentioned_idents = set()
        if not mentioned_target_functions:
            mentioned_target_functions = set()

        spin = Spinner(UPDATING_REPO_MAP_MESSAGE)

        ranked_tags = self.get_ranked_tags(
            chat_fnames,
            other_fnames,
            mentioned_fnames,
            mentioned_idents,
            mentioned_target_functions,
            progress=spin.step,
        )

        other_rel_fnames = sorted(set(self.get_rel_fname(fname) for fname in other_fnames))
        special_fnames = filter_important_files(other_rel_fnames)
        ranked_tags_fnames = set(tag[0] for tag in ranked_tags)
        special_fnames = [fn for fn in special_fnames if fn not in ranked_tags_fnames]
        special_fnames = [(fn,) for fn in special_fnames]

        ranked_tags = special_fnames + ranked_tags

        spin.step()

        num_tags = len(ranked_tags)
        lower_bound = 0
        upper_bound = num_tags
        best_tree = None
        best_tree_tokens = 0

        chat_rel_fnames = set(self.get_rel_fname(fname) for fname in chat_fnames)

        self.tree_cache = dict()

        middle = min(int(max_map_tokens // 25), num_tags)
        while lower_bound <= upper_bound:
            # dump(lower_bound, middle, upper_bound)

            if middle > 1500:
                show_tokens = f"{middle / 1000.0:.1f}K"
            else:
                show_tokens = str(middle)
            spin.step(f"{UPDATING_REPO_MAP_MESSAGE}: {show_tokens} tokens")

            tree = self.to_tree(ranked_tags[:middle], chat_rel_fnames)
            num_tokens = self.token_count(tree)

            pct_err = abs(num_tokens - max_map_tokens) / max_map_tokens
            ok_err = 0.15
            if (num_tokens <= max_map_tokens and num_tokens > best_tree_tokens) or pct_err < ok_err:
                best_tree = tree
                best_tree_tokens = num_tokens

                if pct_err < ok_err:
                    break

            if num_tokens < max_map_tokens:
                lower_bound = middle + 1
            else:
                upper_bound = middle - 1

            middle = int((lower_bound + upper_bound) // 2)

        spin.end()
        return best_tree

    tree_cache = dict()

    def render_tree(self, abs_fname, rel_fname, lois):
        mtime = self.get_mtime(abs_fname)
        key = (rel_fname, tuple(sorted(lois)), mtime)

        if key in self.tree_cache:
            return self.tree_cache[key]

        if (
            rel_fname not in self.tree_context_cache
            or self.tree_context_cache[rel_fname]["mtime"] != mtime
        ):
            code = self.io.read_text(abs_fname) or ""
            if not code.endswith("\n"):
                code += "\n"

            context = TreeContext(
                rel_fname,
                code,
                color=False,
                line_number=False,
                child_context=False,
                last_line=False,
                margin=0,
                mark_lois=False,
                loi_pad=0,
                # header_max=30,
                show_top_of_file_parent_scope=False,
            )
            self.tree_context_cache[rel_fname] = {"context": context, "mtime": mtime}

        context = self.tree_context_cache[rel_fname]["context"]
        context.lines_of_interest = set()
        context.add_lines_of_interest(lois)
        context.add_context()
        res = context.format()
        self.tree_cache[key] = res
        return res

    def render_class_summaries(self, abs_fname, rel_fname, tags):
        class_infos = self._get_python_class_info(abs_fname, rel_fname)
        if not class_infos:
            return ""

        selected_names = set()
        for tag in tags:
            if type(tag) is Tag:
                selected_names.add(tag.name)

        if not selected_names:
            return ""

        relevant_classes = []
        seen = set()
        for class_info in class_infos:
            if (
                class_info.name in selected_names
                or class_info.qualname in selected_names
                or selected_names.intersection(class_info.member_names)
            ):
                key = (class_info.rel_fname, class_info.qualname, class_info.line)
                if key not in seen:
                    seen.add(key)
                    relevant_classes.append(class_info)

        if not relevant_classes:
            return ""

        sections = []
        for class_info in sorted(relevant_classes, key=lambda info: (info.line, info.qualname)):
            lines = [f"# Type summary for class {class_info.qualname}"]
            if class_info.attr_summaries:
                lines.append("attrs:")
                lines.extend(f"  - {summary}" for summary in class_info.attr_summaries)
            if class_info.method_summaries:
                lines.append("methods:")
                lines.extend(f"  - {summary}" for summary in class_info.method_summaries)
            sections.append("\n".join(lines))

        return "\n\n" + "\n\n".join(sections) + "\n"

    def to_tree(self, tags, chat_rel_fnames):
        if not tags:
            return ""

        cur_fname = None
        cur_abs_fname = None
        lois = None
        cur_tags = None
        output = ""

        # add a bogus tag at the end so we trip the this_fname != cur_fname...
        dummy_tag = (None,)
        for tag in sorted(tags) + [dummy_tag]:
            this_rel_fname = tag[0]
            if this_rel_fname in chat_rel_fnames:
                continue

            # ... here ... to output the final real entry in the list
            if this_rel_fname != cur_fname:
                if lois is not None:
                    output += "\n"
                    output += cur_fname + ":\n"
                    output += self.render_tree(cur_abs_fname, cur_fname, lois)
                    output += self.render_class_summaries(cur_abs_fname, cur_fname, cur_tags or [])
                    lois = None
                    cur_tags = None
                elif cur_fname:
                    output += "\n" + cur_fname + "\n"
                if type(tag) is Tag:
                    lois = []
                    cur_tags = []
                    cur_abs_fname = tag.fname
                cur_fname = this_rel_fname

            if lois is not None:
                lois.append(tag.line)
                cur_tags.append(tag)

        # truncate long lines, in case we get minified js or something else crazy
        output = "\n".join([line[:100] for line in output.splitlines()]) + "\n"

        return output


def find_src_files(directory):
    if not os.path.isdir(directory):
        return [directory]

    src_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            src_files.append(os.path.join(root, file))
    return src_files


def get_random_color():
    hue = random.random()
    r, g, b = [int(x * 255) for x in colorsys.hsv_to_rgb(hue, 1, 0.75)]
    res = f"#{r:02x}{g:02x}{b:02x}"
    return res


def get_scm_fname(lang):
    # Load the tags queries
    if USING_TSL_PACK:
        subdir = "tree-sitter-language-pack"
        try:
            path = resources.files(__package__).joinpath(
                "queries",
                subdir,
                f"{lang}-tags.scm",
            )
            if path.exists():
                return path
        except KeyError:
            pass

    # Fall back to tree-sitter-languages
    subdir = "tree-sitter-languages"
    try:
        return resources.files(__package__).joinpath(
            "queries",
            subdir,
            f"{lang}-tags.scm",
        )
    except KeyError:
        return


def get_supported_languages_md():
    from grep_ast.parsers import PARSERS

    res = """
| Language | File extension | Repo map | Linter |
|:--------:|:--------------:|:--------:|:------:|
"""
    data = sorted((lang, ex) for ex, lang in PARSERS.items())

    for lang, ext in data:
        fn = get_scm_fname(lang)
        repo_map = "✓" if Path(fn).exists() else ""
        linter_support = "✓"
        res += f"| {lang:20} | {ext:20} | {repo_map:^8} | {linter_support:^6} |\n"

    res += "\n"

    return res


if __name__ == "__main__":
    fnames = sys.argv[1:]

    chat_fnames = []
    other_fnames = []
    for fname in sys.argv[1:]:
        if Path(fname).is_dir():
            chat_fnames += find_src_files(fname)
        else:
            chat_fnames.append(fname)

    rm = TypeRepoMap(root=".")
    repo_map = rm.get_ranked_tags_map(chat_fnames, other_fnames)

    dump(len(repo_map))
    print(repo_map)
