import matplotlib.pyplot as plt
import json

# 1. 파일 읽기 (예시: 파일명이 data_a.json, data_b.json, data_c.json 일 경우)
with open('random_result.json', 'r') as f: data_a = json.load(f)
with open('order_result.json', 'r') as f: data_b = json.load(f)
with open('copilot_file_result.json', 'r') as f: data_c = json.load(f)
with open('copilot_func_result.json', 'r') as f: data_d = json.load(f)

# 2. 제외할 키 식별 (Version C에서 값이 1.0인 것)
keys_to_exclude_c = [k for k, v in data_c.items() if v == 1.0]
keys_to_exclude_d = [k for k, v in data_d.items() if v == 0.0]
keys_to_exclude = set(keys_to_exclude_c + keys_to_exclude_d)

print(len(keys_to_exclude_c), len(keys_to_exclude_d), len(keys_to_exclude))

a_values = [v for k, v in data_a.items()]
b_values = [v for k, v in data_b.items()]

# 3. 모든 데이터셋에서 해당 키 제외
filtered_a = [v for k, v in data_a.items() if k not in keys_to_exclude]
filtered_b = [v for k, v in data_b.items() if k not in keys_to_exclude]
filtered_c = [v for k, v in data_c.items() if k not in keys_to_exclude]
filtered_d = [v for k, v in data_d.items() if k not in keys_to_exclude]

plt.figure(figsize=(10, 6))
plt.boxplot([a_values, b_values],
            labels=['Aider (Random)', 'Aider (Order)'],)
plt.title('Comparison of Versions (Aider)', fontsize=14)
plt.ylabel('Score Value', fontsize=12)
plt.grid(axis='y', linestyle='--', alpha=0.7)
plt.savefig('comparison_boxplot_aider.png')

# 4. 박스플롯 그리기
plt.figure(figsize=(10, 6))
plt.boxplot([filtered_a, filtered_b, filtered_c, filtered_d], 
            labels=['Aider (Random)', 'Aider (Order)', 'Copilot (File)', 'Copilot (Func)'],)

plt.title('Comparison of Versions (All)', fontsize=14)
plt.ylabel('Score Value', fontsize=12)
plt.grid(axis='y', linestyle='--', alpha=0.7)

plt.savefig('comparison_boxplot.png')