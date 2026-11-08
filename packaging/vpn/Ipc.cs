// 本地 IPC：命名管道 \\.\pipe\offcraft-vpnd + 首行 token 鉴权 + JSON Lines 命令。
//
// 客户端（Python GUI）先写 ``AUTH <token>``，之后每行一个 JSON 命令；服务端每行回一个
// JSON 结果。token 由 vpnd 启动时生成（或由 --token 指定），并落到
// ``%ProgramData%\CrMcDownload\vpn\ipc_token.txt`` 供同机用户读取。
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace CrMcVpn
{
    internal static class JsonUtil
    {
        private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer();

        public static Dictionary<string, object> Parse(string line)
        {
            try
            {
                object value = Serializer.DeserializeObject(line);
                return value as Dictionary<string, object>;
            }
            catch (Exception)
            {
                return null;
            }
        }

        public static string Serialize(object value)
        {
            return Serializer.Serialize(value);
        }

        public static string String(Dictionary<string, object> map, string key)
        {
            if (map == null || !map.ContainsKey(key) || map[key] == null)
            {
                return "";
            }
            return Convert.ToString(map[key]);
        }

        public static int Int(Dictionary<string, object> map, string key)
        {
            if (map == null || !map.ContainsKey(key) || map[key] == null)
            {
                return 0;
            }
            try
            {
                return Convert.ToInt32(map[key]);
            }
            catch (Exception)
            {
                return 0;
            }
        }

        public static bool Bool(Dictionary<string, object> map, string key, bool fallback)
        {
            if (map == null || !map.ContainsKey(key) || map[key] == null)
            {
                return fallback;
            }
            object value = map[key];
            if (value is bool)
            {
                return (bool)value;
            }
            string text = Convert.ToString(value).ToLowerInvariant();
            return text == "1" || text == "true" || text == "yes" || text == "on";
        }

        public static Dictionary<string, object> Map(Dictionary<string, object> map, string key)
        {
            if (map == null || !map.ContainsKey(key))
            {
                return null;
            }
            return map[key] as Dictionary<string, object>;
        }
    }

    internal sealed class IpcServer
    {
        public const string DefaultPipeName = @"\\.\pipe\offcraft-vpnd";
        private readonly VpnEngine _engine;
        private readonly string _token;
        private readonly string _tokenPath;
        private readonly string _pipeName;
        private readonly string _pipeShort;
        private readonly string _configPath;
        private volatile bool _running;
        private Thread _acceptThread;

        public IpcServer(VpnEngine engine, string token, string tokenPath, string pipeName,
            string configPath)
        {
            _engine = engine;
            _token = token;
            _tokenPath = tokenPath;
            _configPath = configPath;
            string name = pipeName.Length > 0 ? pipeName : "offcraft-vpnd";
            // 允许传 "offcraft-vpnd" 或完整 @"\\.\pipe\offcraft-vpnd"，统一收敛成完整路径
            string prefix = @"\\.\pipe\";
            if (name.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                name = name.Substring(prefix.Length);
            }
            _pipeName = prefix + name;
            _pipeShort = name;
        }

        public string Token
        {
            get { return _token; }
        }

        public void Start()
        {
            WriteToken();
            _running = true;
            _acceptThread = new Thread(AcceptLoop);
            _acceptThread.IsBackground = true;
            _acceptThread.Name = "vpn-ipc";
            _acceptThread.Start();
            Log.Write("IPC listening " + _pipeName);
        }

        public void Stop()
        {
            _running = false;
            Thread thread = _acceptThread;
            if (thread != null)
            {
                try
                {
                    thread.Join(2000);
                }
                catch (Exception)
                {
                }
            }
            _acceptThread = null;
        }

        private void WriteToken()
        {
            if (_tokenPath.Length == 0)
            {
                return;
            }
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(_tokenPath));
                File.WriteAllText(_tokenPath, _token, Encoding.ASCII);
            }
            catch (Exception exc)
            {
                Log.Write("写入 IPC token 失败：" + exc.Message);
            }
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                NamedPipeServerStream pipe = null;
                try
                {
                    // .NET 的 NamedPipeServerStream 只接受短名（内部自己加 \\.\pipe\ 前缀），
                    // 传完整路径会被二次拼接成 \\.\pipe\pipe\name，客户端就永远找不到
                    pipe = new NamedPipeServerStream(_pipeShort, PipeDirection.InOut, 4,
                        PipeTransmissionMode.Byte, PipeOptions.None);
                    pipe.WaitForConnection();
                    Log.Write("IPC connected");
                    Thread worker = new Thread(new ThreadStart(delegate { Serve(pipe); }));
                    worker.IsBackground = true;
                    worker.Name = "vpn-ipc-client";
                    worker.Start();
                }
                catch (Exception exc)
                {
                    if (pipe != null)
                    {
                        try
                        {
                            pipe.Dispose();
                        }
                        catch (Exception)
                        {
                        }
                    }
                    if (_running)
                    {
                        Log.Write("IPC 接受连接失败：" + exc.Message);
                        Thread.Sleep(200);
                    }
                }
            }
        }

        private void Serve(NamedPipeServerStream pipe)
        {
            StreamReader reader = new StreamReader(pipe, Encoding.UTF8, false);
            StreamWriter writer = new StreamWriter(pipe, Encoding.UTF8, 4096);
            writer.AutoFlush = true;
            try
            {
                string auth = reader.ReadLine();
                if (auth == null || !CheckAuth(auth))
                {
                    writer.WriteLine("{\"ok\":false,\"error\":\"bad-token\"}");
                    Log.Write("IPC 鉴权失败，已断开");
                    return;
                }
                writer.WriteLine("{\"ok\":true,\"service\":\"vpnd\",\"vip\":\""
                    + Escape(_engine.Vip) + "\"}");
                Log.Write("IPC auth ok");
                while (_running)
                {
                    string line = reader.ReadLine();
                    if (line == null)
                    {
                        break;
                    }
                    if (line.Trim().Length == 0)
                    {
                        continue;
                    }
                    Dictionary<string, object> command = JsonUtil.Parse(line);
                    if (command == null)
                    {
                        writer.WriteLine("{\"ok\":false,\"error\":\"bad-json\"}");
                        continue;
                    }
                    string reply = Handle(command);
                    writer.WriteLine(reply);
                }
            }
            catch (Exception exc)
            {
                // 客户端断开/管道被关闭：静默结束本连接
                if (_running)
                {
                    Log.Write("IPC 会话结束：" + exc.Message);
                }
            }
            finally
            {
                try
                {
                    writer.Flush();
                }
                catch (Exception)
                {
                }
                try
                {
                    pipe.Dispose();
                }
                catch (Exception)
                {
                }
            }
        }

        private bool CheckAuth(string line)
        {
            string prefix = "AUTH ";
            if (line.Length <= prefix.Length || !line.StartsWith(prefix, StringComparison.Ordinal))
            {
                return false;
            }
            string given = line.Substring(prefix.Length).Trim();
            return ConstantTimeEquals(given, _token);
        }

        private static bool ConstantTimeEquals(string a, string b)
        {
            if (a.Length != b.Length)
            {
                return false;
            }
            int diff = 0;
            for (int i = 0; i < a.Length; i++)
            {
                diff |= a[i] ^ b[i];
            }
            return diff == 0;
        }

        private string Handle(Dictionary<string, object> command)
        {
            string cmd = JsonUtil.String(command, "cmd").ToLowerInvariant();
            try
            {
                if (cmd == "ping")
                {
                    return "{\"ok\":true,\"pong\":true}";
                }
                if (cmd == "status")
                {
                    Dictionary<string, object> status = _engine.Status();
                    status["ok"] = true;
                    return JsonUtil.Serialize(status);
                }
                if (cmd == "config")
                {
                    VpnConfig config = VpnConfig.FromJson(command);
                    if (config.Vip.Length == 0)
                    {
                        return "{\"ok\":false,\"error\":\"missing-vip\"}";
                    }
                    _engine.Start(config);
                    PersistConfig(command);
                    Dictionary<string, object> result = new Dictionary<string, object>();
                    result["ok"] = true;
                    result["vip"] = config.Vip;
                    result["udp_port"] = _engine.BoundPort;
                    result["adapter"] = _engine.AdapterReady;
                    result["last_error"] = _engine.LastError;
                    return JsonUtil.Serialize(result);
                }
                if (cmd == "edge")
                {
                    bool ok = _engine.UpdateEdge(JsonUtil.String(command, "host"),
                        JsonUtil.Int(command, "port"), JsonUtil.String(command, "token"));
                    if (!ok)
                    {
                        return "{\"ok\":false,\"error\":\"engine-not-running\"}";
                    }
                    PersistEdge(command);
                    return "{\"ok\":true}";
                }
                if (cmd == "stop")
                {
                    _engine.Stop();
                    PersistConfig(null);
                    return "{\"ok\":true}";
                }
                return "{\"ok\":false,\"error\":\"unknown-cmd\"}";
            }
            catch (Exception exc)
            {
                return "{\"ok\":false,\"error\":\"" + Escape(exc.Message) + "\"}";
            }
        }

        /// <summary>把新边缘令牌写回已落盘配置，服务重启后不必等下一次信令刷新。</summary>
        private void PersistEdge(Dictionary<string, object> command)
        {
            if (_configPath.Length == 0 || !File.Exists(_configPath))
            {
                return;
            }
            try
            {
                Dictionary<string, object> stored = JsonUtil.Parse(File.ReadAllText(_configPath));
                if (stored == null)
                {
                    return;
                }
                Dictionary<string, object> edge = new Dictionary<string, object>();
                edge["host"] = JsonUtil.String(command, "host");
                edge["port"] = JsonUtil.Int(command, "port");
                edge["token"] = JsonUtil.String(command, "token");
                stored["edge"] = edge;
                File.WriteAllText(_configPath, JsonUtil.Serialize(stored), Encoding.UTF8);
            }
            catch (Exception exc)
            {
                Log.Write("保存边缘令牌失败：" + exc.Message);
            }
        }

        /// <summary>把最近一次生效的配置落盘，服务重启后自动恢复；``stop`` 时清除。</summary>
        private void PersistConfig(Dictionary<string, object> command)
        {
            if (_configPath.Length == 0)
            {
                return;
            }
            try
            {
                if (command == null)
                {
                    if (File.Exists(_configPath))
                    {
                        File.Delete(_configPath);
                    }
                    return;
                }
                File.WriteAllText(_configPath, JsonUtil.Serialize(command), Encoding.UTF8);
            }
            catch (Exception exc)
            {
                Log.Write("保存 VPN 配置失败：" + exc.Message);
            }
        }

        private static string Escape(string text)
        {
            if (text == null)
            {
                return "";
            }
            return text.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }
    }

    internal static class IpcToken
    {
        /// <summary>读取或生成 IPC token（32 字节 hex）。</summary>
        public static string LoadOrCreate(string path)
        {
            try
            {
                if (File.Exists(path))
                {
                    string existing = File.ReadAllText(path).Trim();
                    if (existing.Length >= 16)
                    {
                        return existing;
                    }
                }
            }
            catch (Exception)
            {
            }
            byte[] random = new byte[32];
            using (RNGCryptoServiceProvider rng = new RNGCryptoServiceProvider())
            {
                rng.GetBytes(random);
            }
            StringBuilder builder = new StringBuilder(random.Length * 2);
            foreach (byte item in random)
            {
                builder.Append(item.ToString("x2"));
            }
            string token = builder.ToString();
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                File.WriteAllText(path, token, Encoding.ASCII);
            }
            catch (Exception)
            {
            }
            return token;
        }
    }
}
