"""
知识库构建工具
独立运行脚本，用于将 data/ 目录下的所有文档构建为 ChromaDB 向量数据库。
"""

from core.knowledge_base import KnowledgeBase


if __name__ == "__main__":
    print("=" * 50)
    print("  棉花知识库构建工具")
    print("=" * 50)

    try:
        print("\n[1/3] 正在初始化 Embedding 模型与 ChromaDB ...")
        kb = KnowledgeBase()
        print("[1/3] 初始化完成。")

        print("\n[2/3] 开始扫描并处理文档 ...")
        kb.build_vector_db()

        print("\n[3/3] 处理完成！")
        print(f"  向量数据库已保存至 chroma_db/ 目录。")

    except Exception as e:
        print(f"\n❌ 构建失败：{e}")
        print("请检查 data/ 目录是否存在文档，以及依赖是否已安装。")
