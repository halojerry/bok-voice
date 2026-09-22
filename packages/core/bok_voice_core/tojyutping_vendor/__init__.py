"""Vendored ToJyutping(CanCLID,BSD-2-Clause,https://github.com/CanCLID/ToJyutping)。

整体字节级 vendor(与上游 commit 见 VENDORED_FROM),只读不改——汉字→粤拼
转换的唯一依赖,零第三方包、纯 stdlib + 随包 trie.txt(379KB)。升级姿势:
重新整目录覆盖 + 更新 VENDORED_FROM,勿手工改单文件(会失去与上游的可对性)。
"""
