// vpnd：虚拟局域网数据面守护进程入口。
//
// 运行形态：
//   vpnd.exe --service            以 Windows 服务运行（安装器注册时用此参数）
//   vpnd.exe --console            前台调试：启动 IPC，等待 GUI 下发配置
//   vpnd.exe --console --config a.json   直接加载静态配置（同机双实例联调）
//   vpnd.exe --selftest           无网卡自检：HKDF / AES-GCM / 抗重放 / VIP 编解码
//
// 状态目录默认 %ProgramData%\CrMcDownload\vpn（IPC token 与日志），可用 --state 覆盖。
using System;
using System.Collections.Generic;
using System.IO;
using System.ServiceProcess;
using System.Threading;

namespace CrMcVpn
{
    internal static class VpndProgram
    {
        private const string ServiceName = "CrMcDownloadVpn";

        public static int Main(string[] args)
        {
            Dictionary<string, string> options = ParseArgs(args);
            string stateDir = options.ContainsKey("state")
                ? options["state"]
                : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                    "CrMcDownload", "vpn");
            Directory.CreateDirectory(stateDir);
            Log.SetFile(Path.Combine(stateDir, "vpnd.log"));

            if (options.ContainsKey("selftest"))
            {
                try
                {
                    return SelfTest.Run();
                }
                catch (Exception exc)
                {
                    Console.Error.WriteLine("selftest crashed: " + exc.GetType().FullName);
                    return 9;
                }
            }

            VpnEngine engine = new VpnEngine();
            string tokenPath = Path.Combine(stateDir, "ipc_token.txt");
            string token = options.ContainsKey("token")
                ? options["token"]
                : IpcToken.LoadOrCreate(tokenPath);
            IpcServer ipc = new IpcServer(engine, token, options.ContainsKey("token") ? "" : tokenPath,
                options.ContainsKey("pipe") ? options["pipe"] : IpcServer.DefaultPipeName,
                Path.Combine(stateDir, "config.json"));

            if (options.ContainsKey("service"))
            {
                VpndService service = new VpndService(engine, ipc, Path.Combine(stateDir, "config.json"));
                ServiceBase.Run(service);
                return 0;
            }

            // 前台模式：可选静态配置
            ipc.Start();
            if (options.ContainsKey("config"))
            {
                string path = options["config"];
                try
                {
                    Dictionary<string, object> config = JsonUtil.Parse(File.ReadAllText(path));
                    if (config == null)
                    {
                        Log.Write("配置文件不是合法 JSON：" + path);
                        return 2;
                    }
                    if (options.ContainsKey("no-adapter"))
                    {
                        config["use_adapter"] = false;
                    }
                    engine.Start(VpnConfig.FromJson(config));
                }
                catch (Exception exc)
                {
                    Log.Write("加载配置失败：" + exc.Message);
                    return 2;
                }
            }
            else
            {
                ApplyPersistedConfig(engine, Path.Combine(stateDir, "config.json"));
            }
            Log.Write("vpnd 前台运行中，Ctrl+C 退出");
            ManualResetEvent stop = new ManualResetEvent(false);
            ConsoleCancelEventHandler handler = delegate(object sender, ConsoleCancelEventArgs e)
            {
                e.Cancel = true;
                stop.Set();
            };
            Console.CancelKeyPress += handler;
            try
            {
                stop.WaitOne();
            }
            finally
            {
                Console.CancelKeyPress -= handler;
                ipc.Stop();
                engine.Stop();
            }
            return 0;
        }

        /// <summary>服务启动时恢复上一次下发的配置（无则跳过）。</summary>
        internal static void ApplyPersistedConfig(VpnEngine engine, string path)
        {
            if (path.Length == 0 || !File.Exists(path))
            {
                return;
            }
            try
            {
                Dictionary<string, object> config = JsonUtil.Parse(File.ReadAllText(path));
                if (config != null)
                {
                    engine.Start(VpnConfig.FromJson(config));
                    Log.Write("已恢复上次 VPN 配置 vip=" + JsonUtil.String(config, "vip"));
                }
            }
            catch (Exception exc)
            {
                Log.Write("恢复上次配置失败：" + exc.Message);
            }
        }

        private static Dictionary<string, string> ParseArgs(string[] args)
        {
            Dictionary<string, string> options = new Dictionary<string, string>();
            for (int i = 0; i < args.Length; i++)
            {
                string arg = args[i] ?? "";
                if (arg.StartsWith("--", StringComparison.Ordinal))
                {
                    string name = arg.Substring(2);
                    string value = "";
                    int eq = name.IndexOf('=');
                    if (eq >= 0)
                    {
                        value = name.Substring(eq + 1);
                        name = name.Substring(0, eq);
                    }
                    else if (i + 1 < args.Length && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
                    {
                        value = args[i + 1];
                        i++;
                    }
                    options[name.ToLowerInvariant()] = value;
                }
            }
            return options;
        }
    }

    internal sealed class VpndService : ServiceBase
    {
        private readonly VpnEngine _engine;
        private readonly IpcServer _ipc;
        private readonly string _configPath;

        public VpndService(VpnEngine engine, IpcServer ipc, string configPath)
        {
            _engine = engine;
            _ipc = ipc;
            _configPath = configPath;
            ServiceName = "CrMcDownloadVpn";
        }

        protected override void OnStart(string[] args)
        {
            Log.Write("服务启动");
            _ipc.Start();
            VpndProgram.ApplyPersistedConfig(_engine, _configPath);
        }

        protected override void OnStop()
        {
            Log.Write("服务停止");
            _ipc.Stop();
            _engine.Stop();
        }
    }

    /// <summary>无网卡自检：供 CI / 安装后验证二进制与密码学栈可用。</summary>
    internal static class SelfTest
    {
        private static int _failures;

        public static int Run()
        {
            Console.WriteLine("vpnd selftest");
            try
            {
                Console.WriteLine("wintun driver version=" + WintunAdapter.RunningDriverVersion());
            }
            catch (Exception exc)
            {
                Console.WriteLine("wintun probe skipped: " + exc.GetType().Name);
            }
            Step("hkdf", CheckHkdf);
            Step("aes-gcm", CheckAesGcm);
            Step("replay", CheckReplay);
            Step("vip", CheckVipCodec);
            if (_failures == 0)
            {
                Console.WriteLine("SELFTEST PASS");
                return 0;
            }
            Console.WriteLine("SELFTEST FAIL (" + _failures + ")");
            return 1;
        }

        private static void Step(string name, Action check)
        {
            try
            {
                check();
            }
            catch (Exception exc)
            {
                _failures++;
                Console.WriteLine("FAIL " + name + " EXCEPTION " + exc.GetType().FullName
                    + ": " + exc.Message);
            }
        }

        private static void CheckHkdf()
        {
            // RFC 5869 用例 1 输入；期望值由 Python cryptography 的 HKDF 交叉验证
            byte[] ikm = new byte[22];
            for (int i = 0; i < ikm.Length; i++)
            {
                ikm[i] = (byte)0x0b;
            }
            byte[] salt = new byte[] { 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
                0x08, 0x09, 0x0a, 0x0b, 0x0c };
            byte[] okm = Hkdf.Derive(ikm, salt, "", 42);
            string expected = "b2a3d45126d31fb6828ef00d76c6d54e9c2bd4785e49c6ad86e327d89d0de940"
                + "8eeda1cbef2b03f30e05";
            string actual = Hex(okm);
            if (actual != expected)
            {
                Console.WriteLine("  hkdf actual  =" + actual);
                Console.WriteLine("  hkdf expected=" + expected);
            }
            Expect("hkdf-rfc5869", actual == expected);
        }

        private static void CheckAesGcm()
        {
            byte[] key = new byte[32];
            for (int i = 0; i < key.Length; i++)
            {
                key[i] = (byte)i;
            }
            byte[] nonce = new byte[12];
            byte[] aad = System.Text.Encoding.UTF8.GetBytes("vpnd-selftest-aad");
            byte[] plain = System.Text.Encoding.UTF8.GetBytes("0123456789abcdef0123456789abcdef");
            bool ok = false;
            bool tamper = false;
            using (AesGcmKey cipher = new AesGcmKey(key))
            {
                byte[] tag;
                byte[] sealedText = cipher.Seal(nonce, aad, plain, out tag);
                byte[] opened = cipher.Open(nonce, aad, sealedText, tag);
                ok = ByteEquals(opened, plain);
                tag[0] ^= 0xFF;
                try
                {
                    cipher.Open(nonce, aad, sealedText, tag);
                }
                catch (Exception)
                {
                    tamper = true;
                }
            }
            Expect("aes-gcm-roundtrip", ok);
            Expect("aes-gcm-tamper", tamper);
        }

        private static void CheckReplay()
        {
            ReplayWindow window = new ReplayWindow();
            bool ok = window.Accept(1) && window.Accept(3) && !window.Accept(3)
                && window.Accept(2) && !window.Accept(1);
            bool jump = true;
            for (int i = 0; i < 64; i++)
            {
                jump = jump && window.Accept((uint)(100 + i));
            }
            bool old = !window.Accept(2);
            Expect("replay-window", ok);
            Expect("replay-window-jump", jump);
            Expect("replay-window-old", old);
        }

        private static void CheckVipCodec()
        {
            uint value = VpnEngine.VipNumber("10.66.0.254");
            bool ok = value != 0 && VpnEngine.VipText(value) == "10.66.0.254";
            bool bad = VpnEngine.VipNumber("10.66.1") == 0;
            Expect("vip-codec", ok);
            Expect("vip-codec-bad", bad);
        }

        private static void Expect(string name, bool condition)
        {
            if (condition)
            {
                Console.WriteLine("PASS " + name);
            }
            else
            {
                _failures++;
                Console.WriteLine("FAIL " + name);
            }
        }

        private static bool ByteEquals(byte[] a, byte[] b)
        {
            if (a.Length != b.Length)
            {
                return false;
            }
            for (int i = 0; i < a.Length; i++)
            {
                if (a[i] != b[i])
                {
                    return false;
                }
            }
            return true;
        }

        private static string Hex(byte[] data)
        {
            System.Text.StringBuilder builder = new System.Text.StringBuilder(data.Length * 2);
            foreach (byte item in data)
            {
                builder.Append(item.ToString("x2"));
            }
            return builder.ToString();
        }
    }
}
