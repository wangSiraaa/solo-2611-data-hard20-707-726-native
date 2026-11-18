"""仓库标签读取方，仅展示交付物的物理来源，不执行分切。"""
import argparse,json
from pathlib import Path
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('manifest');a=p.parse_args();x=json.loads(Path(a.manifest).read_text())
    if x['schema']!='piece-delivery/v1':raise ValueError('unsupported manifest')
    print(x['delivery_id'],x['piece_id'],x['root_id'],x['root_lo'],x['root_hi'],x['orientation'],x['length'])
