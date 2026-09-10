// 运行时配置注入口：节点本地托管时由 node-agent 覆写本文件（tools/node_agent.py
// write_ui_config）；云端托管保持空对象（走构建期 NEXT_PUBLIC_* 或默认值）。
window.__BOK_CONFIG__ = window.__BOK_CONFIG__ || {};
