"""下游报表读取方：只读取公开评价记录，不负责换算或判定。"""
import argparse,json
from fractions import Fraction
from pathlib import Path
def read_report(path):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if data['schema']!='measurement-report/v1':raise ValueError('unsupported report schema')
    for row in data['evaluations']:
        value=Fraction(row['converted_value']['n'],row['converted_value']['d'])
        print(row['evaluation_id'],row['measurement_id'],row['directory_revision'],str(value),'PASS' if row['passed'] else 'FAIL')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('report');a=p.parse_args();read_report(a.report)
