import os
import shutil
import subprocess
from pathlib import Path
import json
from datetime import datetime

from get_function_list import get_function_list

# ================= 설정 부분 =================
# 1. 원본 프로젝트들이 있는 디렉토리 (건드리지 않음)
SOURCE_ROOT = "/repos/test"

# 2. 작업이 진행될 새로운 디렉토리 (복사본)
WORK_ROOT = "./processed_benchmarks_my_debug"

DATA_PATH = "transformed_result.json"
LOG_ROOT = "./logs/my_debug"  # 로그 파일이 저장될 폴더

# 3. 사용할 로컬 LLM 모델
AIDER_MODEL = "openai/openai/gpt-oss-120b" 

# 4. Aider에게 전달할 프롬프트
PROMPT = "Analyze all the provided files. Add appropriate Python type annotations to all function signatures. You have to annotate only function signature. Use the function logic and call sites to infer the types correctly."

PROMPT_TEMPLATE = """
### Task: Add Type Annotations to a Specific Function
- **Target Function**: {func_name}
- **Objective**: Infer and add Python type hints to the parameters and the return value of the target function.

### Execution Instructions:
1. **Analyze Context**: Before providing the edit, scan the current file for class definitions, imports, variable usages, and other function signatures to determine the most accurate types.
2. **Minimal Edit (Strict)**: Use exactly ONE `SEARCH/REPLACE` block. 
3. **No Redundancy**: Do NOT rewrite the entire file. Include only the function signature and the very beginning of the function body in the `SEARCH` block to keep the diff as small as possible.
4. **Avoid Truncation**: Ensure the `REPLACE` block ends immediately after the modified function signature or the first few lines of the body. Do not attempt to output the rest of the 2,000 lines.
5. **Format**: Strictly follow the aider `SEARCH/REPLACE` format.

Please provide the type hints now.
"""
# ===========================================

START_PROJECT = "albertyw__git-browse"
START_FUNCTION = "test_enum_field_default"
PROJECT_SKIP = True
FILE_SKIP = False

def find_file(project_path, file_path):
    p = project_path / (file_path + ".py")

    if p.exists():
        return file_path + ".py"
    
    p = project_path / "src" / (file_path + ".py")

    if p.exists():
        return "src" + "/" + (file_path + ".py")
    
    p = project_path / file_path / "__init__.py"

    if p.exists():
        return file_path + "/" + "__init__.py"
    
    p = project_path / "src" / file_path / "__init__.py"

    if p.exists():
        return "src" + "/" + file_path + "/" + "__init__.py"

def get_py_files(project_path):
    """프로젝트 폴더 내의 모든 .py 파일 목록을 가져옵니다 (venv 등 제외)."""
    py_files = []
    for path in project_path.rglob("*.py"):
        if any(part in path.parts for part in ["venv", ".venv", "__pycache__", "build", "dist"]):
            continue
        py_files.append(str(path))
    return py_files

def process_project(project_path, log_path):
    global PROJECT_SKIP, FILE_SKIP
    """하나의 프로젝트 전체를 처리합니다."""
    print(f"\n[>>>] Processing project: {project_path.name}")

    if project_path.name == START_PROJECT:
        PROJECT_SKIP = False

    if PROJECT_SKIP:
        return

    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    src_path_set = set()

    for item in data:
        if project_path.name == item['repo_name']:
            src_path = project_path / item['file_path'].split('/')[0]

        
            if not src_path.exists():
                src_path = project_path / "src"

            if not src_path.exists():
                print(f"[!] Source path not found for {project_path.name}")
                return
            
            src_path_set.add(src_path.name)
            break

    py_files = get_py_files(project_path)
    
    if not py_files:
        print(f"No python files found in {project_path}")
        return

    # 1. 모든 파일에서 타입 제거
    print(f"[-] Stripping types from {len(py_files)} files...")
    for file in py_files:
        print(f"    - {file}")
        subprocess.run(["strip-hints", "--inplace", "--to-empty"] + [file], check=True)

    # drop project_path in py_files
    # py_files = [str(Path(file).relative_to(project_path)) for file in py_files]


    function_list = get_function_list(project_path)

    # 2. Aider 실행
    print(f"[+] Running Aider for the entire project...")

    
    with open(log_path, "w", encoding="utf-8") as log_file:
        
        for function in function_list:
            py_file_path = find_file(project_path, function.path.module.replace('.', '/'))
            py_file = str(py_file_path)

            if "git_browse/godocs.py" not in py_file:
                continue

            if not (project_path / py_file).exists():
                print(f"[ERROR] Aider failed for {project_path.name} on file {py_file}. There are no files.")
                continue

            function_name = function.path.path

            if function_name == START_FUNCTION:
                FILE_SKIP = False

            if FILE_SKIP:
                continue

            prompt = PROMPT_TEMPLATE.format(func_name=function_name)
            try:
                print(f"    - Processing file: {py_file} ---> {function_name}")
                cmd = [
                    "aider",
                    "--model", AIDER_MODEL,
                    "--no-suggest-shell-commands",    # 불필요한 제안 차단
                    "--no-attribute-author",
                    "--no-auto-commit",
                    "--no-gitignore",
                    "--no-stream",
                    "--no-show-model-warnings",
                    "--max-chat-history-tokens", "10000",
                    "--map-tokens", "2048",
                    "--edit-format", "diff",
                    "--model-metadata-file", "model_metadata.json",
                    "--message", prompt,
                ] + [py_file] + ["--yes"]
                # stdout과 stderr를 모두 로그 파일로 리다이렉션합니다.
                result =subprocess.run(
                    cmd, 
                    cwd=project_path, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.STDOUT, # 에러 내용도 stdout 파일에 합침
                    check=True,
                    text=True,
                    timeout=300
                )
                print(result.stdout)  # Aider의 출력 결과를 로그에 기록
                log_file.write(result.stdout)
                print(f"    [OK] Project {function_name} completed.")
            except subprocess.CalledProcessError as e:
                print(f"    [ERROR] Aider failed for {project_path.name} on file {py_file}. Check log for details.")
            except subprocess.TimeoutExpired:
                print(f"    [TIMEOUT] Aider timed out for {project_path.name} on file {py_file}.")

def main():
    source_path = Path(SOURCE_ROOT)
    work_path = Path(WORK_ROOT)
    log_path = Path(LOG_ROOT)

    # 1. 작업 디렉토리 생성 (이미 있으면 삭제 후 새로 생성하거나 경고)
    if work_path.exists():
        # response = input(f"'{WORK_ROOT}' already exists. Overwrite? (y/n): ")
        # if response.lower() == 'y':
        shutil.rmtree(work_path)
        # 2. 전체 디렉토리 복사
        print(f"[*] Copying {SOURCE_ROOT} to {WORK_ROOT}...")
        shutil.copytree(source_path, work_path, ignore_dangling_symlinks=True)
        print("[*] Copy complete.")
        # else:
        pass
    else:
        # 2. 전체 디렉토리 복사
        print(f"[*] Copying {SOURCE_ROOT} to {WORK_ROOT}...")
        shutil.copytree(source_path, work_path, ignore_dangling_symlinks=True)
        print("[*] Copy complete.")
        
    if not log_path.exists():
        log_path.mkdir(parents=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 3. 복사된 디렉토리 내의 프로젝트들 순회
    projects = [d for d in work_path.iterdir() if d.is_dir()]
    
    for project_path in projects:
        current_log = log_path / f"{project_path.name}_{timestamp}.log"
        process_project(project_path, current_log)

    print("\n[V] All tasks completed! Original files are safe in", SOURCE_ROOT)

if __name__ == "__main__":
    main()