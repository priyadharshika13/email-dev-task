import os
import ast

PROJECT_PATH = "./"
OUTPUT_FILE = "README_DOCS.md"

def extract_docstrings(py_file):
    with open(py_file, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    module_doc = ast.get_docstring(tree)
    functions = []
    classes = []

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            functions.append((node.name, ast.get_docstring(node)))
        if isinstance(node, ast.ClassDef):
            classes.append((node.name, ast.get_docstring(node)))

    return module_doc, classes, functions


def generate():
    docs = "# Auto-Generated Code Documentation\n\n"

    for root, _, files in os.walk(PROJECT_PATH):
        for file in files:
            if file.endswith(".py"):
                full_path = os.path.join(root, file)
                module_doc, classes, funcs = extract_docstrings(full_path)

                docs += f"## 📄 `{file}`\n"

                if module_doc:
                    docs += f"### Module Description\n```\n{module_doc}\n```\n\n"

                if classes:
                    docs += "### Classes\n"
                    for cls_name, doc in classes:
                        docs += f"#### {cls_name}\n```\n{doc}\n```\n\n"

                if funcs:
                    docs += "### Functions\n"
                    for fn_name, doc in funcs:
                        docs += f"#### {fn_name}()\n```\n{doc}\n```\n\n"

                docs += "---\n"

    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        out.write(docs)

    print("Documentation generated → README_DOCS.md")


if __name__ == "__main__":
    generate()
