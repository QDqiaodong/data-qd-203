# 显式卷积码遥测块

这是离线遥测编码/解码数据API，不连接设备、不声称兼容某行业无线协议。输入constraint为2..7，generators为2..4个不同正整数掩码且<2^constraint；全部参数由调用者提供，不带标准码表。移位约定：register=(state<<1)|input_bit，输出依次为每个(register & generator)的置位数模2，下一状态为register低constraint-1位，初始状态0。code.encode同时输出码字与状态路径。

frames为1..20个唯一id，每个payload_length=1..500、terminated布尔、received数组。terminated=true时发送payload后补constraint-1个0并强制最终状态0；false不补尾且任意终态。received长度必须恰等于(负载长度+尾长)*生成多项式数，元素严格为整数0/1或null（擦除）；不能用bool。每帧独立，不延续前帧状态，不自动识别边界。

解码找全部合法状态路径中最小硬判汉明距离，null不计代价。返回字典序最小的负载、optimal_paths精确计数和ambiguous标志；多条最优路径的码字即使相同也不合并路径。状态、重编码、修正的已知位下标及擦除下标按原0起展开序列保留，distance只含已知错误。不声称歧义选中的负载已确认正确，也不以距离0保证真实信源正确；没有CRC校验或误码能力认证。

所有步骤数有界，使用Viterbi状态递推并保留每状态最优路径计数与字典序代表。没有软判概率、打孔、交织、同步或连续流恢复；未来帧结构、交织和打孔可接入bit index、帧身份和状态证据。样例参考负载101，constraint3/generators7,5，终止码字1110001011，将首位翻转后唯一最优仍101。

API输入错误抛ValueError，整个结果成功计算后可用io.publish创建新文件；拒绝已有目标和链接。examples为固定集成演示，不是通用CLI工具。
