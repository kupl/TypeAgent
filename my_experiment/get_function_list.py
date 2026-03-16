import asyncio
import multiprocessing
from typet5.static_analysis import (
    PythonProject,
    UsageAnalysis,
    PythonFunction
)
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typet5.function_decoding import (
    PreprocessArgs,
    DecodingOrders,
)
from pathlib import Path

DefaultWorkers: int = multiprocessing.cpu_count() // 2

async def project_rollout(
    project: PythonProject,
    pre_args: PreprocessArgs,
    decode_order: "DecodingOrder",
    cpu_executor: ProcessPoolExecutor,
):     
    eloop = asyncio.get_event_loop()
    analysis: UsageAnalysis = await eloop.run_in_executor(
        cpu_executor,
        UsageAnalysis,
        project,
        pre_args.add_override_usages,
        pre_args.add_implicit_rel_imports,
    )
    to_visit = [analysis.path2elem[p] for p in decode_order.traverse(analysis)]

    return to_visit

async def run(project, pre_args, decode_order):
    with ThreadPoolExecutor(1) as model_executor, ProcessPoolExecutor(
        DefaultWorkers
    ) as cpu_executor:
        x = await project_rollout(
            project,
            pre_args,
            decode_order,
            cpu_executor=cpu_executor,
        )
        
        return x
        

def get_function_list(path, random_order=False):
    if random_order:
        decode_order = DecodingOrders.IndependentOrder()
    else:
        decode_order = DecodingOrders.Callee2Caller()
    pre_args = PreprocessArgs()
    project = PythonProject.parse_from_root(path)

    # print(project)

    visit_list = asyncio.run(run(project, pre_args, decode_order))

    visit_list = [v for v in visit_list if isinstance(v, PythonFunction)]

    return visit_list

if __name__ == "__main__":
    get_function_list("")