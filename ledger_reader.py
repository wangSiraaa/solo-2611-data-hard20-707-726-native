"""兑换台账的只读客户；不判断用券资格，不执行扣次/返还。"""
import argparse,json,sqlite3
from pathlib import Path
def snapshot(db):
    con=sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    data={}
    for table,order in [('coupon_instances','instance_id'),('redemptions','order_id'),('redemption_uses','order_id,instance_id'),('cancellations','cancel_id')]:
        data[table]=[dict(row) for row in con.execute(f'SELECT * FROM {table} ORDER BY {order}')]
    con.close();return data
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('db');a=p.parse_args();print(json.dumps(snapshot(a.db),ensure_ascii=False,indent=2))
