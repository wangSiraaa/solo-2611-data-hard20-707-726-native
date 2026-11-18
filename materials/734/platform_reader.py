"""站台只读客户：消费归属记录，不计算编组或坐标。"""
import argparse,json
from pathlib import Path
from fractions import Fraction
def display(x):
    return '-' if x is None else str(Fraction(x['n'],x['d']))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('report');a=p.parse_args();r=json.loads(Path(a.report).read_text())
    if r['schema']!='platform-scan/v1':raise ValueError('unsupported report')
    print(r['report_id'],r['train_id'],r['composition_version'])
    for item in r['cargo_positions']:
        print(item['cargo_id'],item['car_id'],display(item['x']),'IN' if item['in_window'] else 'OUT')
