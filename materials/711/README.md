# 可信封箱事实与交接客户端
initial_store.json含合成的已封箱BOX_A/BOX_B及其可信封箱记录，BOX_M尚开放，已有记录需导入持久库。handoff文件独立存放，是接收方的不可信输入。handoff_client.py仅读取外部交接并形成shipment-input/v1，不判定可信性。运行`python3 handoff_client.py shipment.json`或changed_shipment.json。

封箱工件字段：schema、seal_id、box_id、rule_revision、capacity、contents(item_id/category/weight)、total_weight。contents按item_id排序；字段数值是整数，不以JSON空白或对象键序认定异文。接收服务须与持久库中的可信封箱记录及该箱当前封箱身份核对，不能只检摘要自洽。

手算：BOX_A中A与BOX_B中B分箱，SHIP_1合法；合并类别做整单相容检查会误拒。BOX_M有两件A，r1不能封，r2去掉A-A后可封。a_changed虽总重量与其内容自洽，仍与可信SEAL_A_1不符，必须拒绝且不占箱。r2不重写已有r1封箱事实。
