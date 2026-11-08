// 调用外部命令（netsh / sc）的最小工具；统一捕获输出、隐藏窗口。
using System;
using System.Diagnostics;
using System.Text;

namespace CrMcVpn
{
    internal static class ProcessUtil
    {
        /// <summary>同步执行并返回输出（失败也返回输出，便于日志诊断）。</summary>
        public static string Run(string fileName, string[] args)
        {
            try
            {
                ProcessStartInfo info = new ProcessStartInfo();
                info.FileName = fileName;
                info.UseShellExecute = false;
                info.CreateNoWindow = true;
                info.RedirectStandardOutput = true;
                info.RedirectStandardError = true;
                info.RedirectStandardInput = true;
                if (args != null)
                {
                    for (int i = 0; i < args.Length; i++)
                    {
                        info.Arguments += (i == 0 ? "" : " ") + args[i];
                    }
                }
                using (Process process = Process.Start(info))
                {
                    string stdout = process.StandardOutput.ReadToEnd();
                    string stderr = process.StandardError.ReadToEnd();
                    process.WaitForExit(30000);
                    string text = (stdout + stderr).Trim();
                    if (process.ExitCode != 0 && text.Length == 0)
                    {
                        text = "退出码 " + process.ExitCode;
                    }
                    return text;
                }
            }
            catch (Exception exc)
            {
                return "无法执行 " + fileName + "：" + exc.Message;
            }
        }

        /// <summary>拼接参数（不做转义，调用方保证参数不含空格；需要空格的用引号包裹）。</summary>
        public static string[] Args(params string[] parts)
        {
            return parts;
        }
    }

    internal static class NetShell
    {
        public static string Run(string[] args)
        {
            return ProcessUtil.Run("netsh", args);
        }
    }
}
