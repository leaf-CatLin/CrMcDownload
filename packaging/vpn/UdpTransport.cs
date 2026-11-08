// UDP 数据面：Wintun 收发的 IPv4 包 <-> 加密 UDP 报文。
//
// M2 范围（骨架）：静态对端地址直连、KEEPALIVE 探测、路径状态（idle/direct）。
// M3 在此文件补 PUNCH 打洞与重打洞；M4 在此文件补 ``R <sid>`` 中继兜底。
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace CrMcVpn
{
    /// <summary>单个对端的配置与运行时状态。</summary>
    internal sealed class PeerState
    {
        public string Sid;
        public string Vip;
        public string EndpointText;
        public IPEndPoint Endpoint;
        public AesGcmKey SendKey;
        public AesGcmKey RecvKey;
        public ReplayWindow Replay = new ReplayWindow();
        public uint TxSeq;
        public long TxPackets;
        public long RxPackets;
        public long TxBytes;
        public long RxBytes;
        public DateTime LastRx = DateTime.MinValue;
        public string Path = "idle";
        public bool RxLogged;   // 首次收到合法包时写一行日志，便于现场排障
        public DateTime NextPunch = DateTime.MinValue;   // 下一次发 PUNCH 的时刻
        public DateTime PunchDeadline = DateTime.MinValue;   // 本轮打洞截止时刻
        public DateTime NextPunchRound = DateTime.MinValue;  // 失败后下一次重新打洞
        public DateTime NextKeepAlive = DateTime.MinValue;
        public DateTime NextDirectProbe = DateTime.MinValue;   // 中继期间低频探测直连
        public int RttMs;

        public void Close()
        {
            if (SendKey != null)
            {
                SendKey.Dispose();
                SendKey = null;
            }
            if (RecvKey != null)
            {
                RecvKey.Dispose();
                RecvKey = null;
            }
        }
    }

    /// <summary>IPC 下发的完整运行配置。</summary>
    internal sealed class VpnConfig
    {
        public string Vip = "";
        public string AdapterName = "CrMcDownload VPN";
        public int ListenPort;
        public bool UseAdapter = true;
        public string EdgeHost = "";
        public int EdgePort;
        public string EdgeToken = "";
        public List<PeerState> Peers = new List<PeerState>();

        public static VpnConfig FromJson(Dictionary<string, object> obj)
        {
            VpnConfig config = new VpnConfig();
            config.Vip = JsonUtil.String(obj, "vip");
            string adapter = JsonUtil.String(obj, "adapter");
            if (adapter.Length > 0)
            {
                config.AdapterName = adapter;
            }
            config.ListenPort = JsonUtil.Int(obj, "listen");
            config.UseAdapter = JsonUtil.Bool(obj, "use_adapter", true);
            Dictionary<string, object> edge = JsonUtil.Map(obj, "edge");
            if (edge != null)
            {
                config.EdgeHost = JsonUtil.String(edge, "host");
                config.EdgePort = JsonUtil.Int(edge, "port");
                config.EdgeToken = JsonUtil.String(edge, "token");
            }
            object peersRaw = obj.ContainsKey("peers") ? obj["peers"] : null;
            object[] peers = peersRaw as object[];
            if (peers != null)
            {
                foreach (object item in peers)
                {
                    Dictionary<string, object> map = item as Dictionary<string, object>;
                    if (map == null)
                    {
                        continue;
                    }
                    PeerState peer = new PeerState();
                    peer.Sid = JsonUtil.String(map, "sid");
                    peer.Vip = JsonUtil.String(map, "vip");
                    peer.EndpointText = JsonUtil.String(map, "endpoint");
                    if (peer.EndpointText.Length > 0)
                    {
                        peer.Endpoint = ParseEndpoint(peer.EndpointText);
                    }
                    string tx = JsonUtil.String(map, "tx_key");
                    string rx = JsonUtil.String(map, "rx_key");
                    if (peer.Sid.Length > 0 && tx.Length > 0 && rx.Length > 0)
                    {
                        peer.SendKey = new AesGcmKey(Hex(tx));
                        peer.RecvKey = new AesGcmKey(Hex(rx));
                        config.Peers.Add(peer);
                    }
                    else
                    {
                        peer.Close();
                    }
                }
            }
            return config;
        }

        public static IPEndPoint ParseEndpoint(string text)
        {
            string value = (text ?? "").Trim();
            int split = value.LastIndexOf(':');
            if (split <= 0 || split == value.Length - 1)
            {
                return null;
            }
            IPAddress address;
            if (!IPAddress.TryParse(value.Substring(0, split), out address))
            {
                return null;
            }
            int port;
            if (!int.TryParse(value.Substring(split + 1), out port) || port <= 0 || port > 65535)
            {
                return null;
            }
            return new IPEndPoint(address, port);
        }

        public static byte[] Hex(string value)
        {
            if (value == null || value.Length % 2 != 0)
            {
                return new byte[0];
            }
            byte[] bytes = new byte[value.Length / 2];
            for (int i = 0; i < bytes.Length; i++)
            {
                bytes[i] = Convert.ToByte(value.Substring(i * 2, 2), 16);
            }
            return bytes;
        }
    }

    /// <summary>VPN 引擎：一块 Wintun 网卡 + 一个 UDP socket + N 条对端加密通道（星型）。</summary>
    internal sealed class VpnEngine
    {
        public const byte Ver = 1;
        public const byte TypeData = 0;
        public const byte TypePunch = 1;
        public const byte TypeKeepAlive = 2;
        private const int HeaderBytes = 10;   // ver+type+dst_vip(4)+seq(4)
        private const int TagBytes = 16;
        private const int MaxPacket = 1500;

        private VpnConfig _config;
        private Socket _socket;
        private WintunAdapter _adapter;
        private readonly object _gate = new object();
        private volatile bool _running;
        private Thread _rxThread;
        private Thread _tunThread;
        private Thread _keepThread;
        private string _lastError = "";
        private string _state = "stopped";
        private int _boundPort;
        private DateTime _startedAt = DateTime.MinValue;
        private IPEndPoint _edgeEndPoint;   // 解析后的边缘服务地址（HELLO 与中继用）
        private DateTime _nextHello = DateTime.MinValue;
        private bool _edgeRegistered;

        public string Vip
        {
            get { return _config == null ? "" : _config.Vip; }
        }

        public string AdapterName
        {
            get { return _config == null ? "" : _config.AdapterName; }
        }

        public int BoundPort
        {
            get { return _boundPort; }
        }

        public bool Running
        {
            get { return _running; }
        }

        public string LastError
        {
            get { return _lastError; }
        }

        public bool AdapterReady
        {
            get { return _adapter != null; }
        }

        public void Start(VpnConfig config)
        {
            Stop();
            _config = config;
            _lastError = "";
            _state = "starting";
            _socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
            _socket.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            _socket.Bind(new IPEndPoint(IPAddress.Any, config.ListenPort > 0 ? config.ListenPort : 0));
            _socket.ReceiveTimeout = 1000;
            _boundPort = ((IPEndPoint)_socket.LocalEndPoint).Port;
            ResolveEdge(config);

            if (config.UseAdapter)
            {
                try
                {
                    _adapter = WintunAdapter.OpenOrCreate(config.AdapterName, "CrMcDownload", 0x400000);
                    string netsh = WintunAdapter.ConfigureIpv4(config.AdapterName, config.Vip);
                    if (netsh.Length > 0 && netsh.IndexOf("错误", StringComparison.Ordinal) >= 0)
                    {
                        Log.Write("配置虚拟 IP 返回：" + netsh);
                    }
                }
                catch (Exception exc)
                {
                    _adapter = null;
                    _lastError = "虚拟网卡不可用：" + exc.Message;
                    Log.Write(_lastError);
                }
            }

            _running = true;
            _startedAt = DateTime.Now;
            _state = _adapter != null ? "running" : "degraded";
            _rxThread = new Thread(RxLoop);
            _rxThread.IsBackground = true;
            _rxThread.Name = "vpn-udp-rx";
            _rxThread.Start();
            if (_adapter != null)
            {
                _tunThread = new Thread(TunLoop);
                _tunThread.IsBackground = true;
                _tunThread.Name = "vpn-wintun-rx";
                _tunThread.Start();
            }
            _keepThread = new Thread(KeepAliveLoop);
            _keepThread.IsBackground = true;
            _keepThread.Name = "vpn-keepalive";
            _keepThread.Start();
            Log.Write("engine started vip=" + config.Vip + " udp=" + _boundPort
                + " adapter=" + (_adapter != null ? config.AdapterName : "off")
                + " peers=" + config.Peers.Count
                + (_edgeEndPoint != null ? " edge=" + _edgeEndPoint.ToString() : ""));
        }

        /// <summary>把配置里的边缘服务主机名解析成固定 IPv4 地址（中继与 HELLO 共用）。</summary>
        private void ResolveEdge(VpnConfig config)
        {
            _edgeEndPoint = null;
            _edgeRegistered = false;
            _nextHello = DateTime.MinValue;
            if (config.EdgeHost.Length == 0 || config.EdgePort <= 0)
            {
                return;
            }
            try
            {
                IPAddress[] addresses = Dns.GetHostAddresses(config.EdgeHost);
                foreach (IPAddress address in addresses)
                {
                    if (address.AddressFamily == AddressFamily.InterNetwork)
                    {
                        _edgeEndPoint = new IPEndPoint(address, config.EdgePort);
                        return;
                    }
                }
                Log.Write("边缘服务无 IPv4 地址：" + config.EdgeHost);
            }
            catch (Exception exc)
            {
                Log.Write("解析边缘服务失败：" + exc.Message);
            }
        }

        public void Stop()
        {
            _running = false;
            Socket socket = _socket;
            _socket = null;
            if (socket != null)
            {
                try
                {
                    socket.Close();
                }
                catch (Exception)
                {
                }
            }
            Join(_rxThread);
            Join(_tunThread);
            Join(_keepThread);
            _rxThread = null;
            _tunThread = null;
            _keepThread = null;
            if (_adapter != null)
            {
                _adapter.Dispose();
                _adapter = null;
            }
            if (_config != null)
            {
                foreach (PeerState peer in _config.Peers)
                {
                    peer.Close();
                }
                _config.Peers.Clear();
            }
            _state = "stopped";
            _edgeEndPoint = null;
            _edgeRegistered = false;
        }

        /// <summary>
        /// 热更新边缘令牌/地址：令牌 TTL 到期后由信令重新下发，这里只换令牌并让下一次
        /// KeepAlive 立刻重发 HELLO，不重启 socket（否则已建立的中继路径会短暂断开）。
        /// </summary>
        public bool UpdateEdge(string host, int port, string token)
        {
            VpnConfig config = _config;
            if (config == null || !_running)
            {
                return false;
            }
            string newHost = host ?? "";
            string newToken = token ?? "";
            bool moved = newHost != config.EdgeHost || port != config.EdgePort;
            config.EdgeHost = newHost;
            config.EdgePort = port;
            config.EdgeToken = newToken;
            if (moved)
            {
                ResolveEdge(config);
            }
            else
            {
                _edgeRegistered = false;
                _nextHello = DateTime.MinValue;
            }
            Log.Write("edge updated host=" + newHost + " port=" + port);
            return true;
        }

        private static void Join(Thread thread)
        {
            if (thread == null)
            {
                return;
            }
            try
            {
                thread.Join(2000);
            }
            catch (Exception)
            {
            }
        }

        private void RxLoop()
        {
            byte[] buffer = new byte[65535];
            while (_running)
            {
                Socket socket = _socket;
                if (socket == null)
                {
                    break;
                }
                try
                {
                    IPEndPoint remote = new IPEndPoint(IPAddress.Any, 0);
                    EndPoint endpoint = remote;
                    int size = socket.ReceiveFrom(buffer, ref endpoint);
                    if (size > 0)
                    {
                        if (buffer[0] == Ver)
                        {
                            HandleDatagram(buffer, size, (IPEndPoint)endpoint);
                        }
                        else
                        {
                            HandleEdgeReply(buffer, size);
                        }
                    }
                }
                catch (SocketException exc)
                {
                    if (exc.SocketErrorCode == SocketError.TimedOut
                        || exc.SocketErrorCode == SocketError.WouldBlock
                        || exc.SocketErrorCode == SocketError.ConnectionReset)
                    {
                        // UDP 收到 ICMP 端口不可达（对端还没起）时 Windows 报 ConnectionReset，
                        // 属正常现象：继续等下一包，不刷错误日志
                        continue;
                    }
                    if (!_running)
                    {
                        break;
                    }
                    Log.Write("UDP 接收失败：" + exc.Message);
                    Thread.Sleep(50);
                }
                catch (ObjectDisposedException)
                {
                    break;
                }
            }
        }

        /// <summary>边缘服务控制回包：``OK <sid> <vip>`` 表示端点登记成功，``ERR`` 表示令牌无效。</summary>
        private void HandleEdgeReply(byte[] buffer, int size)
        {
            string text = Encoding.ASCII.GetString(buffer, 0, size).Trim();
            if (text.StartsWith("OK ", StringComparison.Ordinal))
            {
                if (!_edgeRegistered)
                {
                    _edgeRegistered = true;
                    Log.Write("edge registered " + text.Substring(3));
                }
                return;
            }
            if (text.StartsWith("ERR", StringComparison.Ordinal))
            {
                _lastError = "edge token 无效：" + text;
                Log.Write(_lastError);
            }
        }

        private void SendHello()
        {
            Socket socket = _socket;
            if (socket == null || _edgeEndPoint == null || _config == null)
            {
                return;
            }
            string token = _config.EdgeToken;
            if (token.Length == 0)
            {
                return;
            }
            try
            {
                socket.SendTo(Encoding.ASCII.GetBytes("HELLO " + token), _edgeEndPoint);
            }
            catch (Exception exc)
            {
                if (_running)
                {
                    Log.Write("edge HELLO 发送失败：" + exc.Message);
                }
            }
        }

        private void HandleDatagram(byte[] buffer, int size, IPEndPoint remote)
        {
            if (size < HeaderBytes + TagBytes || buffer[0] != Ver)
            {
                return;
            }
            byte type = buffer[1];
            if (type != TypeData && type != TypePunch && type != TypeKeepAlive)
            {
                return;
            }
            uint seq = ReadUInt(buffer, 6);
            byte[] aad = new byte[HeaderBytes];
            Buffer.BlockCopy(buffer, 0, aad, 0, HeaderBytes);
            byte[] tag = new byte[TagBytes];
            Buffer.BlockCopy(buffer, HeaderBytes, tag, 0, TagBytes);
            int cipherBytes = size - HeaderBytes - TagBytes;
            byte[] cipher = new byte[cipherBytes];
            Buffer.BlockCopy(buffer, HeaderBytes + TagBytes, cipher, 0, cipherBytes);
            byte[] nonce = Nonce(seq);

            lock (_gate)
            {
                foreach (PeerState peer in _config.Peers)
                {
                    if (peer.RecvKey == null)
                    {
                        continue;
                    }
                    try
                    {
                        byte[] plain = peer.RecvKey.Open(nonce, aad, cipher, tag);
                        if (!peer.Replay.Accept(seq))
                        {
                            Log.Write("丢弃重放包 sid=" + peer.Sid + " seq=" + seq);
                            return;
                        }
                        peer.LastRx = DateTime.Now;
                        peer.RxPackets++;
                        peer.RxBytes += plain.Length;
                        if ((type == TypePunch || type == TypeKeepAlive) && plain.Length >= 8)
                        {
                            long ticks = BitConverter.ToInt64(plain, 0);
                            long delta = DateTime.UtcNow.Ticks - ticks;
                            if (delta >= 0 && delta < TimeSpan.TicksPerMinute)
                            {
                                peer.RttMs = (int)(delta / TimeSpan.TicksPerMillisecond);
                            }
                        }
                        bool viaEdge = IsEdge(remote);
                        if (viaEdge)
                        {
                            // 中继包不改写 direct：直连仍有效时优先直连
                            if (peer.Path != "direct")
                            {
                                SetPath(peer, "relay", "rx " + type);
                            }
                        }
                        else
                        {
                            SetPath(peer, "direct", "rx " + type);
                        }
                        if (!peer.RxLogged)
                        {
                            peer.RxLogged = true;
                            Log.Write("peer " + peer.Sid + " first packet from "
                                + (remote == null ? "?" : remote.ToString())
                                + " rtt=" + peer.RttMs + "ms");
                        }
                        if (!viaEdge && peer.Endpoint == null && remote != null)
                        {
                            // M3：收到合法包即可视为打洞完成，记录对端实际来源地址
                            peer.Endpoint = remote;
                            peer.EndpointText = remote.ToString();
                        }
                        if (type == TypeData && _adapter != null && plain.Length > 0)
                        {
                            _adapter.Send(plain);
                        }
                        return;
                    }
                    catch (Exception)
                    {
                        // 该密钥不匹配，换下一个对端尝试
                    }
                }
            }
        }

        private void TunLoop()
        {
            while (_running)
            {
                WintunAdapter adapter = _adapter;
                if (adapter == null)
                {
                    break;
                }
                byte[] packet;
                try
                {
                    packet = adapter.Receive(1000);
                }
                catch (Exception exc)
                {
                    if (_running)
                    {
                        Log.Write("Wintun 读取失败：" + exc.Message);
                        Thread.Sleep(100);
                    }
                    continue;
                }
                if (packet == null || packet.Length < 20)
                {
                    continue;
                }
                if ((packet[0] & 0xF0) != 0x40)
                {
                    continue;   // 只转发 IPv4
                }
                string dst = VipText(ReadUInt(packet, 16));
                PeerState peer = FindPeer(dst);
                if (peer == null)
                {
                    continue;   // 非虚拟网段/未知目的：丢弃
                }
                SendPacket(peer, dst, TypeData, packet);
            }
        }

        private void KeepAliveLoop()
        {
            // 500ms 一拍：打洞期间每拍发 PUNCH；直连后每 10s 发 KEEPALIVE；
            // 连续 20s 收不到包则重新打洞；一轮 6s 未成功进入 idle，20s 后重试（M4 接入中继）
            while (_running)
            {
                for (int i = 0; i < 5 && _running; i++)
                {
                    Thread.Sleep(100);
                }
                if (!_running || _config == null)
                {
                    continue;
                }
                DateTime now = DateTime.Now;
                if (_edgeEndPoint != null && now >= _nextHello)
                {
                    _nextHello = now.AddSeconds(60);
                    SendHello();
                }
                foreach (PeerState peer in _config.Peers)
                {
                    if (peer.Path == "relay" || (peer.Endpoint == null && _edgeEndPoint != null))
                    {
                        if (peer.Path != "relay")
                        {
                            SetPath(peer, "relay", "no endpoint");
                            peer.NextKeepAlive = DateTime.MinValue;
                        }
                        bool relayStale = peer.LastRx != DateTime.MinValue
                            && (now - peer.LastRx).TotalSeconds > 20;
                        if (relayStale && peer.Endpoint != null)
                        {
                            SetPath(peer, "punching", "relay timeout");
                            peer.PunchDeadline = now.AddSeconds(6);
                            peer.NextPunch = now;
                        }
                        else if (now >= peer.NextKeepAlive)
                        {
                            peer.NextKeepAlive = now.AddSeconds(10);
                            SendProbe(peer, TypeKeepAlive);
                        }
                        if (peer.Endpoint != null && now >= peer.NextDirectProbe)
                        {
                            // 中继期间每分钟探一次直连；成功收到对端直发包即升级为 direct
                            peer.NextDirectProbe = now.AddSeconds(60);
                            SendPacket(peer, peer.Vip, TypePunch,
                                BitConverter.GetBytes(DateTime.UtcNow.Ticks), false);
                        }
                        continue;
                    }
                    if (peer.Endpoint == null)
                    {
                        continue;
                    }
                    if (peer.Path == "direct")
                    {
                        bool stale = peer.LastRx != DateTime.MinValue
                            && (now - peer.LastRx).TotalSeconds > 20;
                        if (stale)
                        {
                            SetPath(peer, "punching", "keepalive timeout");
                            peer.PunchDeadline = now.AddSeconds(6);
                            peer.NextPunch = now;
                        }
                        else if (now >= peer.NextKeepAlive)
                        {
                            peer.NextKeepAlive = now.AddSeconds(10);
                            SendProbe(peer, TypeKeepAlive);
                        }
                        continue;
                    }
                    if (peer.Path == "idle" || peer.Path == "punching")
                    {
                        if (peer.Path == "idle")
                        {
                            if (now < peer.NextPunchRound)
                            {
                                continue;
                            }
                            SetPath(peer, "punching", "start punch");
                            peer.PunchDeadline = now.AddSeconds(6);
                            peer.NextPunch = now;
                        }
                        if (now <= peer.PunchDeadline)
                        {
                            if (now >= peer.NextPunch)
                            {
                                peer.NextPunch = now.AddMilliseconds(500);
                                SendProbe(peer, TypePunch);
                            }
                        }
                        else
                        {
                            if (_edgeEndPoint != null)
                            {
                                // 打洞失败（含对称 NAT）→ 走中继，并保留低频直连探测
                                SetPath(peer, "relay", "punch timeout -> relay");
                                peer.NextKeepAlive = DateTime.MinValue;
                                peer.NextDirectProbe = now.AddSeconds(60);
                            }
                            else
                            {
                                SetPath(peer, "idle", "punch timeout");
                                peer.NextPunchRound = now.AddSeconds(20);
                                peer.NextKeepAlive = now.AddSeconds(10);
                            }
                        }
                    }
                }
            }
        }

        private void SetPath(PeerState peer, string path, string reason)
        {
            if (peer.Path == path)
            {
                return;
            }
            string previous = peer.Path;
            peer.Path = path;   // 先落状态再写日志，避免另一线程读到旧值
            Log.Write("peer " + peer.Sid + " path " + previous + "->" + path
                + (reason.Length > 0 ? " (" + reason + ")" : ""));
        }

        /// <summary>PUNCH / KEEPALIVE 携带发送时刻（UTC ticks），对端据此算出 RTT。</summary>
        private void SendProbe(PeerState peer, byte type)
        {
            SendPacket(peer, peer.Vip, type, BitConverter.GetBytes(DateTime.UtcNow.Ticks));
        }

        private bool IsEdge(IPEndPoint remote)
        {
            return remote != null && _edgeEndPoint != null
                && remote.Port == _edgeEndPoint.Port
                && remote.Address.Equals(_edgeEndPoint.Address);
        }

        private PeerState FindPeer(string vip)
        {
            if (vip.Length == 0 || _config == null)
            {
                return null;
            }
            foreach (PeerState peer in _config.Peers)
            {
                if (peer.Vip == vip)
                {
                    return peer;
                }
            }
            return null;
        }

        private bool SendPacket(PeerState peer, string dstVip, byte type, byte[] payload)
        {
            return SendPacket(peer, dstVip, type, payload, peer.Path == "relay");
        }

        private bool SendPacket(PeerState peer, string dstVip, byte type, byte[] payload,
            bool viaRelay)
        {
            if (peer.SendKey == null || payload.Length > MaxPacket
                || (!viaRelay && peer.Endpoint == null))
            {
                return false;
            }
            uint seq;
            lock (_gate)
            {
                seq = peer.TxSeq++;
            }
            byte[] header = new byte[HeaderBytes];
            header[0] = Ver;
            header[1] = type;
            WriteUInt(header, 2, VipNumber(dstVip));
            WriteUInt(header, 6, seq);
            byte[] tag;
            byte[] cipher;
            try
            {
                cipher = peer.SendKey.Seal(Nonce(seq), header, payload, out tag);
            }
            catch (Exception exc)
            {
                _lastError = "加密失败：" + exc.Message;
                return false;
            }
            byte[] datagram = new byte[HeaderBytes + TagBytes + cipher.Length];
            Buffer.BlockCopy(header, 0, datagram, 0, HeaderBytes);
            Buffer.BlockCopy(tag, 0, datagram, HeaderBytes, TagBytes);
            Buffer.BlockCopy(cipher, 0, datagram, HeaderBytes + TagBytes, cipher.Length);
            Socket socket = _socket;
            if (socket == null)
            {
                return false;
            }
            IPEndPoint target = peer.Endpoint;
            byte[] outbound = datagram;
            if (viaRelay)
            {
                if (_edgeEndPoint == null)
                {
                    _lastError = "中继需要边缘服务，但配置里没有 edge";
                    return false;
                }
                if (peer.Sid.Length != 32)
                {
                    _lastError = "中继要求 sid 为 32 字符 hex，实际 " + peer.Sid.Length;
                    return false;
                }
                // 协议头 ``R <dst_sid>``（2+32 字节明文前缀），服务端按 sid 查表转发密文
                byte[] prefix = Encoding.ASCII.GetBytes("R " + peer.Sid);
                outbound = new byte[prefix.Length + datagram.Length];
                Buffer.BlockCopy(prefix, 0, outbound, 0, prefix.Length);
                Buffer.BlockCopy(datagram, 0, outbound, prefix.Length, datagram.Length);
                target = _edgeEndPoint;
            }
            try
            {
                socket.SendTo(outbound, target);
                lock (_gate)
                {
                    peer.TxPackets++;
                    peer.TxBytes += payload.Length;
                }
                return true;
            }
            catch (Exception exc)
            {
                _lastError = "UDP 发送失败：" + exc.Message;
                return false;
            }
        }

        public Dictionary<string, object> Status()
        {
            Dictionary<string, object> status = new Dictionary<string, object>();
            status["running"] = _running;
            status["state"] = _state;
            status["vip"] = Vip;
            status["adapter"] = AdapterReady;
            status["adapter_name"] = AdapterName;
            status["udp_port"] = _boundPort;
            status["edge"] = _edgeEndPoint != null ? _edgeEndPoint.ToString() : "";
            status["edge_registered"] = _edgeRegistered;
            status["last_error"] = _lastError;
            status["uptime"] = _running && _startedAt != DateTime.MinValue
                ? (int)(DateTime.Now - _startedAt).TotalSeconds
                : 0;
            List<object> peers = new List<object>();
            if (_config != null)
            {
                lock (_gate)
                {
                    foreach (PeerState peer in _config.Peers)
                    {
                        Dictionary<string, object> item = new Dictionary<string, object>();
                        item["sid"] = peer.Sid;
                        item["vip"] = peer.Vip;
                        item["endpoint"] = peer.EndpointText;
                        item["path"] = peer.Path;
                        item["tx"] = peer.TxPackets;
                        item["rx"] = peer.RxPackets;
                        item["tx_bytes"] = peer.TxBytes;
                        item["rx_bytes"] = peer.RxBytes;
                        item["rtt_ms"] = peer.RttMs;
                        item["last_rx"] = peer.LastRx == DateTime.MinValue
                            ? 0
                            : (long)(DateTime.Now - peer.LastRx).TotalSeconds;
                        peers.Add(item);
                    }
                }
            }
            status["peers"] = peers.ToArray();
            return status;
        }

        // ---------------------------------------------------------- 报文工具
        private static byte[] Nonce(uint seq)
        {
            byte[] nonce = new byte[AesGcmKey.NonceBytes];
            WriteUInt(nonce, 8, seq);
            return nonce;
        }

        private static void WriteUInt(byte[] buffer, int offset, uint value)
        {
            buffer[offset] = (byte)(value >> 24);
            buffer[offset + 1] = (byte)(value >> 16);
            buffer[offset + 2] = (byte)(value >> 8);
            buffer[offset + 3] = (byte)value;
        }

        private static uint ReadUInt(byte[] buffer, int offset)
        {
            return ((uint)buffer[offset] << 24)
                | ((uint)buffer[offset + 1] << 16)
                | ((uint)buffer[offset + 2] << 8)
                | buffer[offset + 3];
        }

        /// <summary>把 10.66.0.x 编码为 32 位整数（便于报文头定长携带，无关大小端）。</summary>
        public static uint VipNumber(string vip)
        {
            string[] parts = (vip ?? "").Split('.');
            if (parts.Length != 4)
            {
                return 0;
            }
            uint value = 0;
            for (int i = 0; i < 4; i++)
            {
                int part;
                if (!int.TryParse(parts[i], out part) || part < 0 || part > 255)
                {
                    return 0;
                }
                value = (value << 8) | (uint)part;
            }
            return value;
        }

        public static string VipText(uint value)
        {
            return ((value >> 24) & 0xFF) + "." + ((value >> 16) & 0xFF) + "."
                + ((value >> 8) & 0xFF) + "." + (value & 0xFF);
        }
    }

    internal static class Log
    {
        private static readonly object _lock = new object();
        private static string _path = "";

        public static void SetFile(string path)
        {
            _path = path;
        }

        public static void Write(string message)
        {
            string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " " + message;
            lock (_lock)
            {
                try
                {
                    Console.WriteLine(line);
                }
                catch (Exception)
                {
                }
                if (_path.Length > 0)
                {
                    try
                    {
                        File.AppendAllText(_path, line + Environment.NewLine, Encoding.UTF8);
                    }
                    catch (Exception)
                    {
                    }
                }
            }
        }
    }
}
