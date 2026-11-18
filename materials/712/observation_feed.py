"""设备文件读取客户；保留输入顺序和观测身份，不解释回绕。"""
import argparse,csv,json
from pathlib import Path
def read_feed(path):
    with Path(path).open(encoding='utf-8',newline='') as f:
        reader=csv.DictReader(f);result=[]
        for row in reader:
            for key in ['time','reading']:row[key]=int(row[key])
            result.append(row)
    return {'schema':'counter-observations/v1','observations':result}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('csv');a=p.parse_args();print(json.dumps(read_feed(a.csv),ensure_ascii=False,indent=2))
