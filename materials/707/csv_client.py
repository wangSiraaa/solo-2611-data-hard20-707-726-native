"""把寄件CSV转换成公开地址输入；不解析目录、不选择站点。"""
import argparse,csv,json
from pathlib import Path
FIELDS=['parcel_id','city_id','district_text','street_text','house_no']
def read_addresses(path):
    with Path(path).open(encoding='utf-8',newline='') as f:
        reader=csv.DictReader(f)
        if reader.fieldnames!=FIELDS: raise ValueError('CSV columns must be '+','.join(FIELDS))
        result=[]
        for source_row,row in enumerate(reader,2):
            if None in row or any(v is None for v in row.values()): raise ValueError(f'row {source_row}: wrong field count')
            raw=row['house_no']
            if not raw.isascii() or not raw.isdecimal() or int(raw)<=0: raise ValueError(f'row {source_row}: house_no must be positive ASCII integer')
            result.append({'source_row':source_row,'address':{**row,'house_no':int(raw)}})
        return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('csv');a=p.parse_args()
    print(json.dumps({'schema':'address-input/v1','rows':read_addresses(a.csv)},ensure_ascii=False,indent=2))
