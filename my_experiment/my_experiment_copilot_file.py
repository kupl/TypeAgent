import os
import shutil
import subprocess
from pathlib import Path
import json
from datetime import datetime
import time

# get_function_list 모듈이 기존과 동일하게 있다고 가정합니다.
from get_function_list import get_function_list

# ================= 설정 부분 =================
SOURCE_ROOT = "./repos/test"
WORK_ROOT = "./processed_benchmarks_copilot_file"
DATA_PATH = "transformed_result.json"
LOG_ROOT = "./logs"

# 2026 Copilot 모델 설정 (GPT-4.1)
COPILOT_MODEL = "gpt-4.1" 

# 에이전트 전용 프롬프트 (SEARCH/REPLACE 제약보다 '정확한 타입 추론'에 집중)
PROMPT_TEMPLATE = """
### Task: Add Type Annotations to Functions
- **Target File**: {file_name}
- **Objective**: Infer and add Python type hints to the parameters and the return value of function signatures.

### Instructions:
1. **Analyze Context**: Use the provided workspace context to understand class definitions and variable usages.
2. **Precision**: Only modify the function signature and add necessary imports if required.
3. **Consistency**: Ensure the types are consistent with the project's existing type hints or coding style.
4. **No Logic Changes**: Do not change the implementation logic of the function.

Please provide the type hints for {file_name} now.
"""
# ===========================================

START_PROJECT = "marcosschroh__dataclasses-avroschema"
START_FUNCTION = "test_enum_field_default"
PROJECT_SKIP = False # 시작 프로젝트 전까지 스킵 여부
FILE_SKIP = False    # 시작 함수 전까지 스킵 여부

def find_file(project_path, file_path):
    # (기존 find_file 로직 유지)
    paths = [
        project_path / (file_path + ".py"),
        project_path / "src" / (file_path + ".py"),
        project_path / file_path / "__init__.py",
        project_path / "src" / file_path / "__init__.py"
    ]
    for p in paths:
        if p.exists():
            return str(p.relative_to(project_path))
    return None

def get_py_files(project_path):
    py_files = []
    for path in project_path.rglob("*.py"):
        if any(part in path.parts for part in ["venv", ".venv", "__pycache__", "build", "dist"]):
            continue
        py_files.append(str(path))
    return py_files

def run_copilot_step(project_path, py_file, function_name, prompt, log_file):
    """gh copilot plan -> task 실행 통합 함수"""
    try:
        # --- PHASE 1: PLAN 생성 시도 ---
        print(f"    Processing: {function_name}")
        task_cmd = [
            "gh", "copilot", "-p",
            prompt,
            "--allow-all-tools",
            "--deny-tool", "shell(rm)",
            "--deny-tool", "shell(mv)",
            "--deny-tool", "shell(git push)",
            "--model", COPILOT_MODEL,
        ]
        # --- PHASE 3: 실제 수정 실행 ---
        log_file.write(f"\n>>> Function: {function_name} ({datetime.now()})\n")
        
        result = subprocess.run(
            task_cmd, 
            cwd=project_path, 
            stdout=log_file, 
            stderr=subprocess.STDOUT, 
            check=True,
            text=True,
            timeout=7200 
        )
        success = True

    except Exception as e:
        print(f"    [ERROR] All attempts failed for {function_name}: {e}")
        success = False
            
    return success

def process_project(project_path, log_path):
    global PROJECT_SKIP, FILE_SKIP
    
    if project_path.name == START_PROJECT:
        PROJECT_SKIP = False
    if PROJECT_SKIP: return

    print(f"\n[>>>] Processing project: {project_path.name}")
    
    # 1. 타입 제거 (연구 환경 초기화)
    py_files = get_py_files(project_path)
    if not py_files: return
    
    print(f"[-] Stripping types from {len(py_files)} files...")
    for file in py_files:
        subprocess.run(["strip-hints", "--inplace", "--to-empty", file], check=True)

    # 2. 함수 목록 추출 및 Copilot 실행
    function_list = get_function_list(project_path)
    modified_py_files = list()

    for function in function_list:
        py_file_path = find_file(project_path, function.path.module.replace('.', '/'))
        py_file = str(py_file_path)

        if py_file not in modified_py_files:
            modified_py_files.append(py_file)

    
    with open(log_path, "a", encoding="utf-8") as log_file:
        for py_file in modified_py_files:
            prompt = PROMPT_TEMPLATE.format(file_name=str(py_file))
            
            success = run_copilot_step(project_path, py_file, py_file, prompt, log_file)
            
            if success:
                print(f"    [OK] {py_file} completed.")
            

def main():
    source_path = Path(SOURCE_ROOT)
    work_path = Path(WORK_ROOT)
    log_path = Path(LOG_ROOT)

    # 작업 디렉토리 복사 로직
    if work_path.exists():
        shutil.rmtree(work_path)
    print(f"[*] Copying {SOURCE_ROOT} to {WORK_ROOT}...")
    shutil.copytree(source_path, work_path)
    
    if not log_path.exists():
        log_path.mkdir(parents=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    projects = sorted([d for d in work_path.iterdir() if d.is_dir()])
    
    for project_path in projects:
        current_log = log_path / f"{project_path.name}_{timestamp}.log"
        process_project(project_path, current_log)

    print("\n[V] All tasks completed!")

if __name__ == "__main__":
    main()