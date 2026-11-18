"""售后查询客户：读取真实额度身份及余额，不推导套餐或执行退货。"""
import argparse,json,sqlite3
from pathlib import Path
def read_credits(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    rows=[dict(x) for x in c.execute('SELECT c.credit_id,c.sale_id,c.slot_id,c.actual_part,c.sold_qty,c.returned_qty,c.sold_qty-c.returned_qty AS remaining,s.package_version FROM component_credits c JOIN sales s USING(sale_id) ORDER BY c.sale_id,c.slot_id')]
    c.close();return {'schema':'component-balances/v1','credits':rows}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('db');a=p.parse_args();print(json.dumps(read_credits(a.db),ensure_ascii=False,indent=2))
