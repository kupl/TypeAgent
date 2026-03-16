import matplotlib.pyplot as plt
import json

# 1. 파일 읽기 (예시: 파일명이 data_a.json, data_b.json, data_c.json 일 경우)
with open('order_result.json', 'r') as f: data_a = json.load(f)
with open('result_my.json', 'r') as f: data_b = json.load(f)

a_values = [v for k, v in data_a.items()]
b_values = [v for k, v in data_b.items()]


plt.figure(figsize=(10, 6))
plt.boxplot([a_values, b_values],
            labels=['Aider (Random)', 'Aider (Order)'],)
plt.title('Comparison of Versions (Aider)', fontsize=14)
plt.ylabel('Score Value', fontsize=12)
plt.grid(axis='y', linestyle='--', alpha=0.7)
plt.savefig('comparison_boxplot_aider.png')

# 4. 박스플롯 그리기
plt.figure(figsize=(10, 6))
plt.boxplot([a_values, b_values], 
            labels=['Aider (Random)', 'My (Random)'],)

plt.title('Comparison of Versions (All)', fontsize=14)
plt.ylabel('Score Value', fontsize=12)
plt.grid(axis='y', linestyle='--', alpha=0.7)

plt.savefig('comparison_boxplot_two.png')