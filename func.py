import io
import json
import logging
import os
from fdk import response

# 导入三个抓取脚本
import fetch_jobs
import fetch_journals
import fetch_reports

def handler(ctx, data: io.BytesIO = None):
    """
    OCI Function 入口函数
    通过 payload 中的 target 参数决定运行哪个脚本:
      {"target": "journals"} → 每天早 6 点，抓昨天论文
      {"target": "reports"}  → 每周五早 7 点，抓过去 7 天报告
      {"target": "jobs"}     → 每周五早 8 点，抓过去 7 天职位
      {"target": "all"}      → 全部运行（手动测试用）
    """
    target = "all"
    try:
        body = json.loads(data.getvalue())
        target = body.get("target", "all")
    except (Exception, ValueError):
        logging.info("未收到 target 参数，默认执行全量任务")

    # 根据目标设置回溯天数
    if target == "journals":
        os.environ["LOOKBACK_DAYS"] = "1"   # 昨天
    elif target in ["reports", "jobs", "all"]:
        os.environ["LOOKBACK_DAYS"] = "7"   # 过去 7 天

    logging.info(f">>> 目标: {target}，LOOKBACK_DAYS={os.environ.get('LOOKBACK_DAYS')}")

    results = {}

    if target in ["journals", "all"]:
        logging.info(">>> 启动 fetch_journals...")
        try:
            fetch_journals.main()
            results["journals"] = "✅ Success"
        except Exception as e:
            logging.error(f"fetch_journals 失败: {e}")
            results["journals"] = f"❌ Error: {e}"

    if target in ["reports", "all"]:
        logging.info(">>> 启动 fetch_reports...")
        try:
            fetch_reports.main()
            results["reports"] = "✅ Success"
        except Exception as e:
            logging.error(f"fetch_reports 失败: {e}")
            results["reports"] = f"❌ Error: {e}"

    if target in ["jobs", "all"]:
        logging.info(">>> 启动 fetch_jobs...")
        try:
            fetch_jobs.main()
            results["jobs"] = "✅ Success"
        except Exception as e:
            logging.error(f"fetch_jobs 失败: {e}")
            results["jobs"] = f"❌ Error: {e}"

    logging.info(f"完成: {results}")

    return response.Response(
        ctx,
        response_data=json.dumps(results, ensure_ascii=False),
        headers={"Content-Type": "application/json"}
    )
