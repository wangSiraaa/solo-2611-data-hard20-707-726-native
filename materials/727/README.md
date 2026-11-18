# 合成墙钟材料
clock.json的整数轴与offset完全定义本题时钟，不指代真实国家时区，也不依赖系统时区库。local=UTC+offset，覆盖区间均半开。requests.json给真实点查询与会话输入，revision.json替换同一区的整份转换表。

手算r1：local90→UTC30，local200无解，local330→UTC210/270。J1起点local300较早解UTC180，终点local330较晚解UTC270，持续90，费用180。r2起点较早解UTC210、终点唯一解UTC270，持续60，费用120；E1仍保存90/180。J_GAP在r1应拒绝。
