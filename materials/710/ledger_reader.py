"""领退料台账只读客户；不选择替代、不修改库存。"""
import argparse,json,sqlite3
from pathlib import Path
def read_ledger(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    result={}
    for table,order in [('inventory','part_id'),('work_order_lines','work_order_id,line_id'),('issues','issue_id'),('issue_lines','issue_id,line_id'),('returns','return_id'),('return_lines','return_id,line_id')]:
        result[table]=[dict(x) for x in c.execute(f'SELECT * FROM {table} ORDER BY {order}')]
    c.close();return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('db');a=p.parse_args();print(json.dumps(read_ledger(a.db),ensure_ascii=False,indent=2))
