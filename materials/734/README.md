# 有序编组与货位
所有编组头到尾定义自己的正方向。车厢orientation=+1时固有local_position从当前列首方向量，-1时映为length-local_position。couple保持target原方向，接other的哪端决定other序列是否整列反转。

T尾接S尾后：[V1+,V2+,V3-]，货位C1=2、C2=12、C3=28，站台[0,5)命中C1。整列reverse后：[V3+,V2-,V1-]，货位2、18、28对应C3/C2/C1，站台改命中C3。只反序不翻每车朝向会算错后两件。

运行`python3 platform_reader.py reader-example.json`确认下游格式。example仅格式示例；目标实际R1/R2报告应采用platform-scan/v1，保留report_id/train_id/composition_version与cargo_positions中cargo_id/car_id/x/in_window。x以整数n、正整数d的分数对象输出，可增字段。
