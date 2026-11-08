// CrMCDownload setup / uninstaller.
// Self-contained: the payload zip is embedded as a managed resource and is
// extracted straight into the target directory, so the setup never needs a
// writable %TEMP% (unlike Inno Setup / PyInstaller onefile bootstrappers).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace CrMcSetup
{
    internal static class Program
    {
        internal const string AppExe = "CrMCDownload.exe";
        internal const string UninstallExe = "uninstall.exe";
        internal const string PayloadResource = "payload.zip";
        internal const string UninstallerResource = "uninstaller.exe";
        internal const string ArpKey =
            @"Software\Microsoft\Windows\CurrentVersion\Uninstall\CrMCDownload";

        internal static string TargetDir = "";
        internal static bool Silent;
        internal static bool UninstallMode;
        internal static bool LaunchApp = true;
        internal static bool DesktopIcon = true;

        [STAThread]
        private static int Main(string[] args)
        {
            bool badArg = false;
            string badText = "";
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i].Trim();
                if (a.Length == 0) continue;
                string u = a.ToUpperInvariant();
                if (u == "/S" || u == "/SILENT" || u == "/VERYSILENT" || u == "/QUIET" ||
                    u == "-S" || u == "--SILENT")
                {
                    Silent = true;
                }
                else if (u == "/UNINSTALL" || u == "/U" || u == "--UNINSTALL")
                {
                    UninstallMode = true;
                }
                else if (u == "/NOLAUNCH")
                {
                    LaunchApp = false;
                }
                else if (u == "/NODESKTOPICON")
                {
                    DesktopIcon = false;
                }
                else if (u.StartsWith("/DIR="))
                {
                    TargetDir = a.Substring(5).Trim('"');
                }
                else if (u == "/?" || u == "/HELP")
                {
                    MessageBox.Show(
                        "CrMCDownload " + BuildInfo.Version + " 安装程序\n\n" +
                        "用法：setup.exe [/S] [/DIR=<目录>] [/NOLAUNCH] [/NODESKTOPICON]\n" +
                        "      setup.exe /UNINSTALL [/S]\n\n" +
                        "发布者：" + BuildInfo.Publisher,
                        "CrMCDownload Setup", MessageBoxButtons.OK,
                        MessageBoxIcon.Information);
                    return 0;
                }
                else if (a.StartsWith("/") || a.StartsWith("-"))
                {
                    badArg = true;
                    badText = a;
                }
            }

            if (TargetDir.Length == 0)
            {
                TargetDir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    @"Programs\" + BuildInfo.Name);
            }
            try
            {
                TargetDir = Path.GetFullPath(TargetDir);
            }
            catch (Exception)
            {
                TargetDir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    @"Programs\" + BuildInfo.Name);
            }

            if (badArg && !Silent)
            {
                DialogResult dr = MessageBox.Show(
                    "无法识别的参数：" + badText + "\n\n是否继续安装？", "CrMCDownload Setup",
                    MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                if (dr != DialogResult.OK && dr != DialogResult.Yes) return 2;
            }

            if (Silent)
            {
                return UninstallMode
                    ? DoUninstall(null, false)
                    : DoInstall(null, DesktopIcon, LaunchApp, false);
            }

            // 双击运行时先把“环境不让写 C 盘”这类问题摆在面前，
            // 而不是让用户点完「安装」才看到报错。
            if (!UninstallMode)
            {
                string problem = EnvProblem();
                if (problem != null)
                {
                    MessageBox.Show(problem, BuildInfo.Name + " " + BuildInfo.Version + " 安装程序",
                        MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 1;
                }
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (SetupForm form = new SetupForm(UninstallMode))
            {
                Application.Run(form);
                return form.ExitCode;
            }
        }

        // ---------------------------------------------------------------- install
        internal static int DoInstall(Action<int, string> report, bool desktopIcon,
                                      bool launch, bool interactive)
        {
            try
            {
                Report(report, 0, "准备安装…");
                Preflight();
                Trace("install start -> " + TargetDir);
                CloseRunning();
                Report(report, 3, "正在创建安装目录…");
                Directory.CreateDirectory(TargetDir);
                ExtractPayload(report);
                Trace("payload extracted");
                Report(report, 96, "正在写入卸载程序…");
                CopySelf();
                Trace("self copied");
                Report(report, 98, "正在创建快捷方式…");
                CreateShortcuts(desktopIcon);
                Trace("shortcuts ok");
                Report(report, 99, "正在登记卸载信息…");
                WriteArp();
                Trace("arp ok");
                Report(report, 100, "安装完成");
                ClearTrace();
                if (launch) Launch();
                return 0;
            }
            catch (Exception ex)
            {
                Fail(ex, interactive, "安装失败");
                return 1;
            }
        }

        internal static int DoUninstall(Action<int, string> report, bool interactive)
        {
            try
            {
                Report(report, 0, "准备卸载…");
                CloseRunning();
                Report(report, 20, "正在删除快捷方式…");
                DeleteShortcuts();
                Report(report, 40, "正在清理注册表…");
                DeleteArp();
                Report(report, 50, "正在卸载 VPN 组件…");
                UninstallVpn();
                Report(report, 60, "正在删除文件…");
                if (!DeleteDirectory(TargetDir))
                {
                    ScheduleDelete(TargetDir);
                }
                Report(report, 100, "卸载完成");
                return 0;
            }
            catch (Exception ex)
 {
                Fail(ex, interactive, "卸载失败");
                return 1;
            }
        }

        /// <summary>
        /// 调用随包分发的 vpn-setup.exe 清理服务 / 虚拟网卡 / 防火墙规则。
        /// 用户未启用过联机（未安装 VPN 组件）时静默跳过；拒绝 UAC 也不阻断主程序卸载。
        /// </summary>
        private static void UninstallVpn()
        {
            // PyInstaller 6 onedir 把非主程序文件放在 _internal，两处都要查
            string setup = Path.Combine(TargetDir, "vpn-setup.exe");
            if (!File.Exists(setup))
            {
                setup = Path.Combine(TargetDir, "_internal", "vpn-setup.exe");
            }
            if (!File.Exists(setup))
            {
                return;
            }
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(setup, "/uninstall");
                psi.UseShellExecute = true;      // runas 需要 ShellExecute
                psi.Verb = "runas";
                psi.WorkingDirectory = TargetDir;
                psi.WindowStyle = ProcessWindowStyle.Hidden;
                Process process = Process.Start(psi);
                if (process != null)
                {
                    process.WaitForExit(15000);
                }
            }
            catch (Exception)
            {
                // 拒绝提权 / 服务未安装：继续删除安装目录即可
            }
        }

        // ------------------------------------------------------------------ pieces
        private static void Trace(string text)
        {
            AppendLog("setup-trace.log",
                DateTime.Now.ToString("HH:mm:ss.fff") + "  " + text + Environment.NewLine);
        }

        internal static string LogDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "CrMCDownload");
            }
        }

        internal static string ExeDir
        {
            get
            {
                try
                {
                    return Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
                }
                catch (Exception)
                {
                    return null;
                }
            }
        }

        // 受限环境下 C:\Users\...\AppData 可能不可写（实测：从 E:\root\WorkSpaces\ 工作区
        // 启动的程序一律被拒绝写入 C 盘），此时把日志落到安装包所在目录，
        // 否则用户双击后只会看到「什么都没发生」。
        private static void AppendLog(string fileName, string text)
        {
            string[] dirs = new string[] { LogDir, ExeDir, Path.GetTempPath() };
            for (int i = 0; i < dirs.Length; i++)
            {
                try
                {
                    if (string.IsNullOrEmpty(dirs[i])) continue;
                    Directory.CreateDirectory(dirs[i]);
                    File.AppendAllText(Path.Combine(dirs[i], fileName), text);
                    return;
                }
                catch (Exception) { }
            }
        }

        // 先探测关键位置能不能写，给出可执行的中文提示，
        // 而不是让用户看到一句 “Access to the path ... is denied”。
        internal static string EnvProblem()
        {
            List<string> bad = new List<string>();
            string startMenu = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                @"Microsoft\Windows\Start Menu\Programs");
            ProbeWritable(NearestExisting(TargetDir), TargetDir, bad);
            ProbeWritable(NearestExisting(LogDir), LogDir, bad);
            ProbeWritable(NearestExisting(startMenu), startMenu, bad);
            if (bad.Count == 0) return null;

            StringBuilder sb = new StringBuilder();
            sb.AppendLine("无法写入以下位置：");
            for (int i = 0; i < bad.Count; i++) sb.AppendLine("  " + bad[i]);
            sb.AppendLine();
            sb.AppendLine("常见原因：安装包被放在了受限目录（例如 E:\\root\\WorkSpaces\\ 下的项目工作区），");
            sb.AppendLine("从这种目录启动的程序会被系统拒绝写入 C 盘。");
            sb.AppendLine();
            sb.AppendLine("解决办法：把安装包复制到「下载」文件夹或桌面，再从那里双击运行。");
            return sb.ToString().TrimEnd();
        }

        private static void Preflight()
        {
            string problem = EnvProblem();
            if (problem != null) throw new UnauthorizedAccessException(problem);
        }

        // 不创建目标目录本身，只探测它最近的已存在上级，
        // 免得用户点了取消却在磁盘上留下空目录。
        private static string NearestExisting(string dir)
        {
            try
            {
                while (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                {
                    dir = Path.GetDirectoryName(dir);
                }
                return dir;
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static void ProbeWritable(string dir, string label, List<string> bad)
        {
            try
            {
                if (string.IsNullOrEmpty(dir)) return;
                Directory.CreateDirectory(dir);
                string probe = Path.Combine(dir, ".crmc-probe.tmp");
                File.WriteAllText(probe, "ok");
                File.Delete(probe);
            }
            catch (Exception)
            {
                string show = string.IsNullOrEmpty(label) ? dir : label;
                if (!bad.Contains(show)) bad.Add(show);
            }
        }

        private static void ClearTrace()
        {
            try
            {
                string p = Path.Combine(LogDir, "setup-trace.log");
                if (File.Exists(p)) File.Delete(p);
                // 上一次失败留下的错误日志也别再挂着，以免误导。
                string e = Path.Combine(LogDir, "setup-error.log");
                if (File.Exists(e)) File.Delete(e);
            }
            catch (Exception) { }
        }
        private static void Report(Action<int, string> report, int pct, string text)
        {
            if (report != null) report(pct, text);
        }

        private static void Fail(Exception ex, bool interactive, string title)
        {
            AppendLog("setup-error.log",
                DateTime.Now.ToString("s") + "  " + ex + Environment.NewLine);
            Trace("ERROR " + ex);
            if (interactive)
            {
                MessageBox.Show(ex.Message, title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private static void ExtractPayload(Action<int, string> report)
        {
            Assembly asm = Assembly.GetExecutingAssembly();
            using (Stream src = asm.GetManifestResourceStream(PayloadResource))
            {
                if (src == null)
                {
                    throw new InvalidOperationException("安装包内缺少 payload 资源，文件已损坏。");
                }
                using (ZipArchive zip = new ZipArchive(src, ZipArchiveMode.Read))
                {
                    long total = 0;
                    for (int i = 0; i < zip.Entries.Count; i++) total += zip.Entries[i].Length;
                    if (total <= 0) total = 1;
                    long done = 0;
                    int n = 0;
                    for (int i = 0; i < zip.Entries.Count; i++)
                    {
                        ZipArchiveEntry e = zip.Entries[i];
                        n++;
                        string rel = e.FullName.Replace('/', Path.DirectorySeparatorChar);
                        string path = Path.Combine(TargetDir, rel);
                        if (e.FullName.EndsWith("/") || e.Name.Length == 0)
                        {
                            Directory.CreateDirectory(path);
                            continue;
                        }
                        string dir = Path.GetDirectoryName(path);
                        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                        try
                        {
                            e.ExtractToFile(path, true);
                        }
                        catch (IOException io)
                        {
                            throw new IOException(
                                "无法写入 " + path + "\n请先关闭正在运行的 " + AppExe +
                                " 后重试。\n\n" + io.Message);
                        }
                        catch (UnauthorizedAccessException ua)
                        {
                            throw new IOException("没有权限写入 " + path + "\n\n" + ua.Message);
                        }
                        done += e.Length;
                        int pct = 3 + (int)(done * 92 / total);
                        if (pct > 95) pct = 95;
                        Report(report, pct, "正在解压文件 " + n + "/" + zip.Entries.Count + " …");
                    }
                }
            }
        }

        private static void CopySelf()
        {
            string dst = Path.Combine(TargetDir, UninstallExe);
            string self = Assembly.GetExecutingAssembly().Location;
            if (string.Equals(self, dst, StringComparison.OrdinalIgnoreCase)) return;
            Assembly asm = Assembly.GetExecutingAssembly();
            using (Stream s = asm.GetManifestResourceStream(UninstallerResource))
            {
                if (s != null)
                {
                    using (FileStream fs = new FileStream(dst, FileMode.Create,
                                                          FileAccess.Write, FileShare.None))
                    {
                        s.CopyTo(fs);
                    }
                    return;
                }
            }
            File.Copy(self, dst, true);
        }

        private static void Launch()
        {
            string exe = Path.Combine(TargetDir, AppExe);
            if (!File.Exists(exe)) return;
            ProcessStartInfo psi = new ProcessStartInfo(exe);
            psi.WorkingDirectory = TargetDir;
            psi.UseShellExecute = true;
            Process.Start(psi);
        }

        internal static void LaunchAppNow()
        {
            try
            {
                Launch();
            }
            catch (Exception)
            {
            }
        }

        private static void CloseRunning()
        {
            Process[] list = Process.GetProcessesByName(
                Path.GetFileNameWithoutExtension(AppExe));
            foreach (Process p in list)
            {
                try
                {
                    string path = "";
                    try { path = p.MainModule.FileName; }
                    catch (Exception) { path = ""; }
                    if (path.Length > 0 &&
                        !path.StartsWith(TargetDir, StringComparison.OrdinalIgnoreCase)) continue;
                    p.CloseMainWindow();
                    if (!p.WaitForExit(6000)) p.Kill();
                    p.WaitForExit(4000);
                }
                catch (Exception) { }
                finally { try { p.Dispose(); } catch (Exception) { } }
            }
        }

        internal static void CreateShortcuts(bool desktopIcon)
        {
            string exe = Path.Combine(TargetDir, AppExe);
            string programs = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
            string group = Path.Combine(programs, BuildInfo.Name);
            Directory.CreateDirectory(group);
            MakeShortcut(Path.Combine(group, BuildInfo.Name + ".lnk"), exe);
            if (desktopIcon)
            {
                string desk = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                MakeShortcut(Path.Combine(desk, BuildInfo.Name + ".lnk"), exe);
            }
        }

        internal static string ShortcutGroup
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.Programs),
                    BuildInfo.Name);
            }
        }

        private static void DeleteShortcuts()
        {
            try
            {
                string group = ShortcutGroup;
                if (Directory.Exists(group)) Directory.Delete(group, true);
            }
            catch (Exception) { }
            try
            {
                string desk = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                string lnk = Path.Combine(desk, BuildInfo.Name + ".lnk");
                if (File.Exists(lnk)) File.Delete(lnk);
            }
            catch (Exception) { }
        }

        private static void MakeShortcut(string lnkPath, string target)
        {
            object shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            try
            {
                Type st = shell.GetType();
                object sc = st.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod,
                    null, shell, new object[] { lnkPath });
                try
                {
                    Type ct = sc.GetType();
                    ct.InvokeMember("TargetPath", BindingFlags.SetProperty, null, sc,
                        new object[] { target });
                    ct.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, sc,
                        new object[] { Path.GetDirectoryName(target) });
                    ct.InvokeMember("IconLocation", BindingFlags.SetProperty, null, sc,
                        new object[] { target + ",0" });
                    ct.InvokeMember("Description", BindingFlags.SetProperty, null, sc,
                        new object[] { "CrMCDownload Minecraft 启动器" });
                    ct.InvokeMember("Save", BindingFlags.InvokeMethod, null, sc, null);
                }
                finally { if (sc != null) Marshal.ReleaseComObject(sc); }
            }
            finally { if (shell != null) Marshal.ReleaseComObject(shell); }
        }

        private static long DirSize(string dir)
        {
            long total = 0;
            try
            {
                string[] files = Directory.GetFiles(dir, "*", SearchOption.AllDirectories);
                for (int i = 0; i < files.Length; i++)
                {
                    try { total += new FileInfo(files[i]).Length; }
                    catch (Exception) { }
                }
            }
            catch (Exception) { }
            return total;
        }

        private static void WriteArp()
        {
            using (RegistryKey k = Registry.CurrentUser.CreateSubKey(ArpKey))
            {
                string exe = Path.Combine(TargetDir, AppExe);
                string un = Path.Combine(TargetDir, UninstallExe);
                k.SetValue("DisplayName", BuildInfo.Name + " " + BuildInfo.Version);
                k.SetValue("DisplayVersion", BuildInfo.Version);
                k.SetValue("Publisher", BuildInfo.Publisher);
                k.SetValue("DisplayIcon", exe + ",0");
                k.SetValue("InstallLocation", TargetDir);
                k.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd"));
                k.SetValue("UninstallString", "\"" + un + "\" /uninstall");
                k.SetValue("QuietUninstallString", "\"" + un + "\" /uninstall /S");
                long kb = DirSize(TargetDir) / 1024;
                if (kb > 2L * 1024 * 1024) kb = 2L * 1024 * 1024;
                if (kb < 1) kb = 1;
                k.SetValue("EstimatedSize", (int)kb, RegistryValueKind.DWord);
                k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            }
        }

        private static void DeleteArp()
        {
            try
            {
                Registry.CurrentUser.DeleteSubKeyTree(ArpKey, false);
            }
            catch (Exception) { }
        }

        private static bool DeleteDirectory(string dir)
        {
            try
            {
                if (Directory.Exists(dir)) Directory.Delete(dir, true);
                return !Directory.Exists(dir);
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static void ScheduleDelete(string dir)
        {
            ProcessStartInfo psi = new ProcessStartInfo("cmd.exe",
                "/c ping -n 3 127.0.0.1 >nul & rd /s /q \"" + dir + "\"");
            psi.CreateNoWindow = true;
            psi.UseShellExecute = false;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            Process.Start(psi);
        }
    }

    internal sealed class SetupForm : Form
    {
        internal int ExitCode;

        private readonly bool _uninstall;
        private readonly Label _title;
        private readonly Label _sub;
        private readonly ProgressBar _bar;
        private readonly CheckBox _desktop;
        private readonly Button _primary;
        private readonly Button _secondary;
        private bool _done;

        internal SetupForm(bool uninstall)
        {
            _uninstall = uninstall;
            Text = BuildInfo.Name + " " + BuildInfo.Version +
                   (uninstall ? " 卸载程序" : " 安装程序");
            ClientSize = new Size(560, 276);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Microsoft YaHei UI", 9F);
            BackColor = Color.FromArgb(250, 250, 252);

            _title = new Label();
            _title.Text = uninstall ? "卸载 CrMCDownload" : "安装 CrMCDownload";
            _title.Font = new Font("Microsoft YaHei UI", 15F, FontStyle.Bold);
            _title.SetBounds(24, 18, 500, 34);
            Controls.Add(_title);

            _sub = new Label();
            _sub.Text = "发布者：" + BuildInfo.Publisher + "    版本：" + BuildInfo.Version;
            _sub.ForeColor = Color.FromArgb(96, 100, 112);
            _sub.SetBounds(26, 56, 500, 22);
            Controls.Add(_sub);

            Label cap = new Label();
            cap.Text = uninstall ? "将从此目录删除程序文件：" : "安装到：";
            cap.ForeColor = Color.FromArgb(96, 100, 112);
            cap.SetBounds(26, 92, 500, 20);
            Controls.Add(cap);

            Label pathBox = new Label();
            pathBox.Text = Program.TargetDir;
            pathBox.AutoEllipsis = true;
            pathBox.SetBounds(26, 112, 508, 22);
            pathBox.ForeColor = Color.FromArgb(40, 44, 56);
            Controls.Add(pathBox);

            _bar = new ProgressBar();
            _bar.SetBounds(24, 152, 512, 14);
            _bar.Minimum = 0;
            _bar.Maximum = 100;
            Controls.Add(_bar);

            _desktop = new CheckBox();
            _desktop.Text = "创建桌面快捷方式";
            _desktop.Checked = true;
            _desktop.SetBounds(24, 182, 240, 24);
            _desktop.Visible = !uninstall;
            Controls.Add(_desktop);

            _primary = new Button();
            _primary.Text = uninstall ? "卸载" : "安装";
            _primary.SetBounds(340, 224, 90, 30);
            _primary.Click += OnPrimary;
            Controls.Add(_primary);

            _secondary = new Button();
            _secondary.Text = "取消";
            _secondary.SetBounds(440, 224, 90, 30);
            _secondary.Click += OnSecondary;
            Controls.Add(_secondary);

            AcceptButton = _primary;
            CancelButton = _secondary;
        }

        private void OnSecondary(object sender, EventArgs e)
        {
            Close();
        }

        private void OnPrimary(object sender, EventArgs e)
        {
            if (_done)
            {
                if (!_uninstall) Program.LaunchAppNow();
                Close();
                return;
            }
            _primary.Enabled = false;
            _secondary.Enabled = false;
            _desktop.Enabled = false;
            int rc = _uninstall
                ? Program.DoUninstall(Report, true)
                : Program.DoInstall(Report, _desktop.Checked, false, true);
            ExitCode = rc;
            _done = true;
            if (rc != 0)
            {
                Close();
                return;
            }
            if (_uninstall)
            {
                Close();
                return;
            }
            _title.Text = "安装完成";
            _sub.Text = BuildInfo.Name + " " + BuildInfo.Version + " 已安装完成。";
            _bar.Value = 100;
            _primary.Text = "启动 CrMCDownload";
            _primary.Enabled = true;
            _secondary.Text = "关闭";
            _secondary.Enabled = true;
        }

        private void Report(int pct, string text)
        {
            if (pct < _bar.Minimum) pct = _bar.Minimum;
            if (pct > _bar.Maximum) pct = _bar.Maximum;
            _bar.Value = pct;
            if (text != null) _sub.Text = text;
            Application.DoEvents();
        }
    }
}
