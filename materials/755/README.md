仅有合成初态、请求、独立手写格式样例及只读客户端，没有库存/交接旧后端。
python3 handoff_reader.py example_handoff.json
asset-handoff/1给handoff_id/membership_revision/roots/from/to/members，每成员含asset_id/type；可加字段。样例EXAMPLE_ONLY不代表已发生交接。

C1内有C2/I2，C2内有I1。扫描C1又扫C2时展开是C1/C2/I1/I2四个身份，不能计五个或把两容器消失成两件货。H1把这四资产从A交B；C3本来已是B。
随后I1移到C3，不自动改其保管人B；H1冻结成员仍四个。当前C1子树只有C1/C2/I2，H2只将这三个从B交C；I1与C3仍为B。独立客户端读H1应仍见I1。
这是逐资产保管责任与物理嵌套身份，不是SKU余额、箱体相容或运输装载优化。
