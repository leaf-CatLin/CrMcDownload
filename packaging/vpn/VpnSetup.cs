// vpn-setup.exe：提权安装/卸载 VPN 组件（服务注册 + 文件分发 + 防火墙规则 + 网卡清理）。
//
// manifest 为 requireAdministrator：只有首次启用联机时 GUI 才会拉起一次 UAC。
// 安装器本体（Setup.cs）保持 asInvoker，不使用联机的用户不受影响。
using System;
using System.IO;
using System.Threading;

namespace CrMcVpn
{
    internal static class VpnSetupProgram
    {
        private const string ServiceName = "CrMcDownloadVpn";
        private const string AdapterName = "CrMcDownload VPN";
        private const string FirewallRule = "CrMcDownload VPN";

        public static int Main(string[] args)
        {
            bool uninstall = false;
            foreach (string arg in args)
            {
                string value = (arg ?? "").ToLowerInvariant();
                if (value == "/uninstall" || value == "-uninstall" || value == "--uninstall")
                {
                    uninstall = true;
                }
            }
            try
            {
                return uninstall ? Uninstall() : Install();
            }
            catch (Exception exc)
            {
                Console.Error.WriteLine("失败：" + exc.Message);
                return 1;
            }
        }

        private static string InstallDir
        {
            get
            {
                return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                    "CrMcDownload", "vpn");
            }
        }

        private static string StateDir
        {
            get
            {
                return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                    "CrMcDownload", "vpn");
            }
        }

        private static int Install()
        {
            string source = AppDomain.CurrentDomain.BaseDirectory;
            Directory.CreateDirectory(InstallDir);
            Directory.CreateDirectory(StateDir);
            CopyIfExists(Path.Combine(source, "vpnd.exe"), Path.Combine(InstallDir, "vpnd.exe"));
            CopyIfExists(Path.Combine(source, "wintun.dll"), Path.Combine(InstallDir, "wintun.dll"));
            CopyIfExists(Path.Combine(source, "LICENSE.txt"), Path.Combine(InstallDir, "LICENSE.txt"));

            string exe = Path.Combine(InstallDir, "vpnd.exe");
            if (!File.Exists(exe))
            {
                Console.Error.WriteLine("缺少 vpnd.exe（应与 vpn-setup.exe 同目录）");
                return 2;
            }

            // 幂等：先停掉旧服务再重建，避免升级时 binPath 指向旧目录
            ProcessUtil.Run("sc", new string[] { "stop", ServiceName });
            Thread.Sleep(500);
            ProcessUtil.Run("sc", new string[] { "delete", ServiceName });
            Thread.Sleep(500);

            string binPath = "binPath= \"" + exe + "\" --service";
            string create = ProcessUtil.Run("sc", new string[]
            {
                "create", ServiceName, binPath, "start= auto", "DisplayName= CrMcDownload VPN",
            });
            if (create.IndexOf("FAILED", StringComparison.OrdinalIgnoreCase) >= 0
                || create.IndexOf("失败", StringComparison.Ordinal) >= 0)
            {
                Console.Error.WriteLine("注册服务失败：" + create);
                return 3;
            }
            ProcessUtil.Run("sc", new string[]
            {
                "description", ServiceName,
                "\"CrMcDownload 虚拟局域网数据面（Wintun + UDP）\"",
            });
            // 崩溃自动重启（24h 内失败计数重置）
            ProcessUtil.Run("sc", new string[]
            {
                "failure", ServiceName, "reset= 86400",
                "actions= restart/5000/restart/5000/restart/5000",
            });
            ProcessUtil.Run("sc", new string[] { "start", ServiceName });

            string program = "program=\"" + exe + "\"";
            ProcessUtil.Run("netsh", new string[]
            {
                "advfirewall firewall add rule name=\"" + FirewallRule + "\" dir=in action=allow "
                    + program + " enable=yes profile=any",
            });

            Console.WriteLine("已安装：" + exe);
            Console.WriteLine("服务：" + ServiceName + "（自动启动，崩溃自动重启）");
            Console.WriteLine("虚拟网卡：" + AdapterName + "（由 vpnd 启动时创建，IP 由 GUI 下发）");
            return 0;
        }

        private static int Uninstall()
        {
            ProcessUtil.Run("sc", new string[] { "stop", ServiceName });
            ProcessUtil.Run("sc", new string[] { "delete", ServiceName });
            ProcessUtil.Run("netsh", new string[]
            {
                "advfirewall firewall delete rule name=\"" + FirewallRule + "\"",
            });
            // 移除虚拟网卡（Windows 8+ 的 Remove-NetAdapter）
            ProcessUtil.Run("powershell", new string[]
            {
                "-NoProfile -NonInteractive -Command \"Remove-NetAdapter -Name '" + AdapterName
                    + "' -Confirm:$false -ErrorAction SilentlyContinue\"",
            });

            DeleteDirectory(InstallDir);
            DeleteDirectory(StateDir);
            Console.WriteLine("已卸载 VPN 组件（服务 / 网卡 / 防火墙规则 / 文件）");
            return 0;
        }

        private static void CopyIfExists(string from, string to)
        {
            if (File.Exists(from))
            {
                File.Copy(from, to, true);
            }
        }

        private static void DeleteDirectory(string path)
        {
            try
            {
                if (Directory.Exists(path))
                {
                    Directory.Delete(path, true);
                }
            }
            catch (Exception exc)
            {
                Console.WriteLine("清理目录失败（可能被占用）：" + path + " - " + exc.Message);
            }
        }
    }
}
