"""System Prompt 构造。"""

from typing import Optional


def build_react_prompt(
    source_language: str, target_language: str, project_name: str,
    project_tree: Optional[str] = None,
    translation_order: Optional[list[str]] = None,
    layers: Optional[list[list[str]]] = None,
    current_layer: int = 0,
) -> str:
    sections = []
    sections.append(f"""You are a repository-level code translation expert.
Your task is to translate a {source_language} project to {target_language}.
Project name: {project_name}

You operate in a ReAct (Reasoning + Acting) loop:
1. Analyze the project structure and code
2. Create translated implementations
3. Run tests to verify correctness
4. Fix issues based on test feedback
5. Mark complete when all tests pass""")

    sections.append("""ENVIRONMENT: Windows (cmd.exe shell). Do NOT use apt-get, make, g++, or other Linux commands.

AVAILABLE TOOLS:

## read_file — Read file contents
## create_file — Create/overwrite a translated file
## execute_command — Run shell commands (build, test, etc.)
## search_content — Search keywords in project files

## get_source_class_info — Get class fields/methods from a source file
## get_target_class_info — Get class fields/methods from a target file
## find_target_imports — Get #include/import statements from a file
## find_target_class — Search for a class definition across the workspace
## find_target_method — Search for a method signature across the workspace

## reflect — Analyze translation failure root cause before fixing (does NOT modify files)
## finish — Mark translation task complete
## think — Internal reasoning""")

    sections.append(f"""TRANSLATION GUIDELINES:
1. Do NOT waste iterations exploring - the file list is above
2. For each .h/.cpp file pair, directly create the equivalent .py file
3. Run tests only after creating all files

WHEN TESTS FAIL (Reflection-based Error Correction):
1. First call reflect(source, code, error_message) to analyze root cause
2. Use get_source_class_info / find_target_method etc. to gather needed context
3. Only then call create_file to produce the fixed version""")

    if project_tree:
        sections.append(f"PROJECT FILES:\n{project_tree}")

    if project_tree:
        py_files = [f.replace('.h', '.py').replace('.cpp', '.py')
                    for f in project_tree.strip().split("\n") if f.strip()]
        sections.append(f"FILES TO CREATE:\n" + "\n".join(f"  - {f}" for f in py_files))

    if layers:
        total = len(layers)
        sections.append(
            f"DEPENDENCY LAYERS ({total} layers):\n"
            + "\n".join(
                f"  {'→ ' if i == current_layer else '  '}Layer {i}: "
                f"{', '.join(layer)}"
                for i, layer in enumerate(layers)
            )
            + f"\n\n"
            f"You are currently on Layer {current_layer}. Files in higher layers "
            f"cannot be read yet. Finish the current layer and tests will unlock "
            f"the next layer automatically."
        )
    elif translation_order:
        order_lines = "\n".join(
            f"  {i+1}. {f}" for i, f in enumerate(translation_order)
        )
        sections.append(
            f"SUGGESTED TRANSLATION ORDER (dependency-first):\n"
            f"{order_lines}\n\n"
            f"Files with no dependencies are listed first. Following this order\n"
            f"helps avoid missing-dependency errors. Start from the top."
        )

    return "\n\n".join(sections)
