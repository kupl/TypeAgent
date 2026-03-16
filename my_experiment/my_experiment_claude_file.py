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
WORK_ROOT = "./processed_benchmarks_claude_file"
DATA_PATH = "transformed_result.json"
LOG_ROOT = "./logs/claude"

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

Add necessary imports (from typing import ...) if they are missing.
Please provide the type hints for {file_name} now.
"""
# ===========================================

START_PROJECT = "TomerFi__aioswitcher"
START_FILE = "tests/test_device_tools.py"
PROJECT_SKIP = True # 시작 프로젝트 전까지 스킵 여부
FILE_SKIP = True    # 시작 함수 전까지 스킵 여부


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

def run_claude_step(project_path, py_file, prompt, log_file):
    """Claude Code CLI 실행 함수"""
    try:
        print(f"    [Processing] {py_file}")
        
        # Claude Code 명령어 구성
        # -y: 모든 수정 및 명령어 실행 자동 승인 (자동화의 핵심)
        # --print: UI 애니메이션 없이 텍스트만 출력하여 로그 기록에 최적화
        cmd = [
            "claude",
            prompt,
            "--permission-mode", "bypassPermissions",
            # "--dangerously-skip-permissions",
            "--print",
            "--output-format", "json",
        ]
        
        log_file.write(f"\n\n>>> File: {py_file} ({datetime.now()})\n")
        log_file.flush()
        
        result = subprocess.run(
            cmd, 
            cwd=project_path, 
            stdout=log_file, 
            stderr=subprocess.STDOUT, 
            check=True,
            text=True,
            timeout=1800 # 파일당 최대 30분 제한 (프로젝트 크기에 따라 조절)
        )

        json_result = json.loads(result.stdout)
        if json_result.get("is_error"):
            print(f"    [ERROR] Claude reported an error for {py_file}. Check log for details.")
            return None

        return True

    except subprocess.TimeoutExpired:
        print(f"    [TIMEOUT] {py_file} exceeded time limit.")
        return False
    except Exception as e:
        print(f"    [ERROR] Failed for {py_file}: {e}")
        return False

def process_project(project_path, log_path):
    global PROJECT_SKIP, FILE_SKIP
    print(f"\n[>>>] Processing project: {project_path.name}")

    if project_path.name == START_PROJECT:
        PROJECT_SKIP = False
    elif not PROJECT_SKIP:
        # 처리해야할 프로젝트들
        py_files = get_py_files(project_path)
        if not py_files: return
        
        print(f"[-] Stripping types from {len(py_files)} files...")
        for file in py_files:
            subprocess.run(["strip-hints", "--inplace", "--to-empty", file], check=True)
    
    if PROJECT_SKIP: return


    
    
    # 1. 타입 제거 (연구 환경 초기화)
    # py_files = get_py_files(project_path)
    # if not py_files: return
    
    # print(f"[-] Stripping types from {len(py_files)} files...")
    # for file in py_files:
    #     subprocess.run(["strip-hints", "--inplace", "--to-empty", file], check=True)

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
            if py_file == START_FILE:
                FILE_SKIP = False

            if FILE_SKIP:
                print(f"    [SKIP] {py_file}")
                continue
            
            prompt = PROMPT_TEMPLATE.format(file_name=str(py_file))
            
            success = run_claude_step(project_path, py_file, prompt, log_file)
            
            if success is None:
                print(f"    [Error] {py_file} due to Claude error.")
                return

            if success:
                print(f"    [OK] {py_file} completed.")
            

def main():
    source_path = Path(SOURCE_ROOT)
    work_path = Path(WORK_ROOT)
    log_path = Path(LOG_ROOT)

    # 작업 디렉토리 복사 로직
    if work_path.exists():
        # shutil.rmtree(work_path)
        pass
    else:
        print(f"[*] Copying {SOURCE_ROOT} to {WORK_ROOT}...")
        shutil.copytree(source_path, work_path, ignore_dangling_symlinks=True)

    if not log_path.exists():
        log_path.mkdir(parents=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    projects = sorted([d for d in work_path.iterdir() if d.is_dir()])
    
    for project_path in projects:
        if "basilisp-lang__basilisp" in str(project_path):
            continue
        current_log = log_path / f"{project_path.name}_{timestamp}.log"
        process_project(project_path, current_log)

    print("\n[V] All tasks completed!")

if __name__ == "__main__":
    main()