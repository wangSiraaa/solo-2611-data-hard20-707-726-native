"""发运接入客户端：读取外部交接请求及凭据，未实现可信库核对。"""
import argparse,json
from pathlib import Path
def read_request(path):
    path=Path(path);request=json.loads(path.read_text(encoding='utf-8'))
    if request['schema']!='handoff-request/v1':raise ValueError('unsupported request')
    manifests=[]
    for name in request['manifest_files']:
        manifest=json.loads((path.parent/name).read_text(encoding='utf-8'))
        if manifest['schema']!='sealed-box/v1':raise ValueError('unsupported manifest')
        for key in ['seal_id','box_id','rule_revision','capacity','contents','total_weight']:manifest[key]
        manifests.append(manifest)
    return {'schema':'shipment-input/v1','shipment_id':request['shipment_id'],'manifests':manifests}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('request');a=p.parse_args();print(json.dumps(read_request(a.request),ensure_ascii=False,indent=2))
