import os
import shutil
import subprocess
from pathlib import Path
import json
from datetime import datetime
import ast
from concurrent.futures import ProcessPoolExecutor, as_completed
from typet5.type_check import parse_type_str
from get_function_list import get_function_list
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm 

# DONE = [
#     "ohjames__babies",
#     "brettkromkamp__topic-db",
# ]

PROCESS_PROJECT = "basilisp-lang__basilisp"

# 1. 원본 프로젝트들이 있는 디렉토리 (건드리지 않음)
SOURCE_ROOT = "./repos/test"

# 2. 작업이 진행될 새로운 디렉토리 (복사본)
WORK_ROOT = "./processed_benchmarks_typergent"

DATA_PATH = "transformed_result.json"

def find_file(project_path, file_path):
    p = project_path / (file_path + ".py")

    if p.exists():
        return p
    
    p = project_path / "src" / (file_path + ".py")

    if p.exists():
        return p
    
    p = project_path / file_path / "__init__.py"

    if p.exists():
        return p
    
    p = project_path / "src" / file_path / "__init__.py"

    if p.exists():
        return p

# Make Type Annotation map via AST
class TypeAnnotationCollector(ast.NodeVisitor):
    def __init__(self):
        self.annotations = {}

    def visit_FunctionDef(self, node):
        func_name = node.name
        args = []
        for arg in node.args.args:
            if arg.annotation:
                arg_type = ast.unparse(arg.annotation)
            else:
                arg_type = None
            args.append((arg.arg, arg_type))

        if node.returns:
            return_type = ast.unparse(node.returns)
        else:
            return_type = None
        self.annotations[func_name] = {
            "args": args,
            "return": return_type
        }
        self.generic_visit(node)

def get_py_files(project_path):
    """프로젝트 폴더 내의 모든 .py 파일 목록을 가져옵니다 (venv 등 제외)."""
    py_files = []
    for path in project_path.rglob("*.py"):
        if any(part in path.parts for part in ["venv", ".venv", "__pycache__", "build", "dist"]):
            continue
        py_files.append(str(path))
    return py_files

def process_single_project(project_path_str, source_root, work_root, data_path):
    """
    개별 프로젝트의 정답률 리스트(correct_list, count_list)와 
    전체 통계(total_num, correct_num)를 반환합니다.
    """
    project_path = Path(project_path_str)
    source_path = Path(source_root)
    work_path = Path(work_root)
    
    correct_list = []
    count_list = []
    total_num = 0
    correct_num = 0

    diff_dict = {}

    original_path = source_path / (project_path.name.replace("_typergent", ""))
    original_py_files = get_py_files(original_path)

    aider_path = work_path / project_path.name
    aider_py_files = get_py_files(aider_path)

    total_num = 0
    correct_num = 0

    function_list = get_function_list(original_path)

    answer_dict = {}

    # Compaer type annotation in function signatures
    for orig_file, aider_file in zip(original_py_files, aider_py_files):
        with open(orig_file, "r", encoding="utf-8") as f:
            orig_code = f.read()
        with open(aider_file, "r", encoding="utf-8") as f:
            aider_code = f.read()

        func_answer_dict = {}

        # Compare Type Annotation via AST
        orig_tree = ast.parse(orig_code)
        try:
            aider_tree = ast.parse(aider_code)
        except:
            print(aider_file, "Not Open")

        orig_collector = TypeAnnotationCollector()
        aider_collector = TypeAnnotationCollector()

        orig_collector.visit(orig_tree)
        aider_collector.visit(aider_tree)

        
        for func_name, orig_types in orig_collector.annotations.items():
            stats = {'count': 0, 'correct': 0}

            for (orig_arg, orig_type) in orig_types['args']:
                if orig_type is not None:
                    total_num += 1
                    stats['count'] += 1

            aider_types = aider_collector.annotations.get(func_name)
            if not aider_types:
                # print(f"[{project_path.name}] Function '{func_name}' not found in aider file '{aider_file}'")
                continue

            # Compare argument types
            for (orig_arg, orig_type), (aider_arg, aider_type) in zip(orig_types['args'], aider_types['args']):
                if orig_arg != aider_arg:
                    # print(orig_types['args'], aider_types['args'])
                    continue
                
                assert orig_arg == aider_arg, f"Argument names do not match: {orig_arg} != {aider_arg}"
                
                if orig_type == None:
                    continue

                try:
                    normalize_orig_type = str(parse_type_str(orig_type).normalized())
                    normalize_aider_type = str(parse_type_str(aider_type).normalized())
                except:
                    continue

                if normalize_orig_type == normalize_aider_type:
                    correct_num += 1
                    stats['correct'] += 1
                else:
                    diff_dict[str(Path.home() / aider_file)] = {
                        "orig_file": str(Path.home() / orig_file),
                        "func_name": func_name,
                        "arg": orig_arg,
                        "orig_type": normalize_orig_type,
                        "aider_type": normalize_aider_type
                    }
                    # print(normalize_orig_type, normalize_aider_type)
                    pass

            # Compare return type
            if orig_types['return'] != None:
                stats['count'] += 1
                total_num += 1

                try:
                    normalize_orig_type = str(parse_type_str(orig_types['return']).normalized())
                    normalize_aider_type = str(parse_type_str(aider_types['return']).normalized())
                except:
                    continue
                if normalize_orig_type == normalize_aider_type:
                    correct_num += 1
                    stats['correct'] += 1
                else:
                    diff_dict[str(Path.home() / aider_file)] = {
                        "orig_file": str(Path.home() / orig_file),
                        "func_name": func_name,
                        "orig_return": normalize_orig_type,
                        "aider_return": normalize_aider_type
                    }
                    pass

            func_answer_dict[func_name] = stats
        answer_dict[orig_file] = func_answer_dict

    for func in function_list:
        py_file_path = find_file(original_path, func.path.module.replace('.', '/'))
        py_file = str(py_file_path)

        function_name = func.path.path

        assert py_file in answer_dict, f"{py_file} vs {answer_dict} \n{func.path.module}, {func.path.path}"
        function_dict = answer_dict[py_file]
        
        last_name = function_name.split('.')[-1]

        if last_name in function_dict:
            stats = function_dict[last_name]

            correct_list.append(stats['correct'])
            count_list.append(stats['count'])

    with open(f"analysis/{project_path.name}_diff.json", "w", encoding="utf-8") as f:
        json.dump(diff_dict, f, indent=4)

    # 각 프로젝트의 구간 정답률 요약 계산
    df_temp = pd.DataFrame({'correct': correct_list, 'total': count_list})
    df_temp['interval'] = pd.cut(range(len(df_temp)), bins=4, labels=["Q1(0-25%)", "Q2(25-50%)", "Q3(50-75%)", "Q4(75-100%)"])
    summary = df_temp.groupby('interval', observed=False).agg({'correct':'sum', 'total':'sum'})
    
    res_dict = (summary['correct'] / summary['total'] * 100).to_dict()
    res_dict['project_id'] = project_path.name
    
    return {
        'res_dict': res_dict,
        'total_num': total_num,
        'correct_num': correct_num,
        'project_name': project_path.name
    }

def main():
    source_path = Path(SOURCE_ROOT)
    work_path = Path(WORK_ROOT)
    # 처리할 프로젝트 목록
    projects = [d for d in work_path.iterdir() if d.is_dir()]
    
    # filter projects
    new_projects = []
    for p in projects:
        if "typergent" not in p.name:
            continue
        # if PROCESS_PROJECT == str(p).split('/')[-1]:
        #     break

        new_projects.append(p)
    projects = new_projects

    all_project_total_num = 0
    all_project_correct_num = 0
    all_project_results = []

    result_dict = {}

    print(f"[*] Starting parallel processing with {os.cpu_count()} cores...")

    # 1. 병렬 실행을 위한 Executor 설정
    with ProcessPoolExecutor(max_workers=24) as executor:
        # 2. 작업 예약 (submit)
        futures = {
            executor.submit(process_single_project, p, SOURCE_ROOT, WORK_ROOT, DATA_PATH): p 
            for p in projects
        }
        
        # 3. tqdm으로 감싸서 진행 상황 보기
        # as_completed(futures)는 작업이 완료되는 순서대로 yield 합니다.
        for future in tqdm(as_completed(futures), total=len(projects), desc="Processing Projects"):
            project_name = Path(futures[future]).name
            try:
                result = future.result()
                
                # 결과 취합
                all_project_results.append(result['res_dict'])
                all_project_total_num += result['total_num']
                all_project_correct_num += result['correct_num']

                result_dict[project_name] = round(result['correct_num']/result['total_num'] if result['total_num'] > 0 else 0, 2)
                
                # tqdm 바 옆에 현재 완료된 프로젝트 이름 표시 (선택 사항)
                tqdm.write(f"[{result['project_name']}] Accuracy: {(result['correct_num']/result['total_num']*100 if result['total_num'] > 0 else 0):.2f}%")
                
            except Exception as e:
                print(f"\n[!] Error processing project {project_name}: {e}")


    with open('result.json', 'w') as f:
        json.dump(result_dict, f, indent=4)

    analysis_df = pd.DataFrame(all_project_results)
    
    # 분석 데이터가 있을 때만 시각화
    if not analysis_df.empty:
        correlation = analysis_df['Q1(0-25%)'].corr(analysis_df['Q4(75-100%)'])
        print(f"\n기초(Q1)와 심화(Q4) 구간의 상관계수: {correlation:.4f}")
        
        sns.regplot(data=analysis_df, x='Q1(0-25%)', y='Q4(75-100%)')
        plt.figure(figsize=(8, 6))
        sns.regplot(data=analysis_df, x='Q1(0-25%)', y='Q4(75-100%)')
        plt.title(f'Correlation between Early and Late Accuracy\n(Corr: {correlation:.4f})')
        plt.xlabel('Early Stage Accuracy (0-25%)')
        plt.ylabel('Late Stage Accuracy (75-100%)')
        plt.grid(True, alpha=0.3)
        plt.savefig('test.png')

    overall_accuracy = (all_project_correct_num / all_project_total_num * 100) if all_project_total_num > 0 else 0.0
    print(f"\n[Overall] Type Annotation Accuracy: {overall_accuracy:.2f}%")

if __name__ == "__main__":
    main()