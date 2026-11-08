// Wintun（L3 虚拟网卡）P/Invoke 封装：创建/打开适配器、启动会话、收发 IPv4 包。
//
// wintun.dll 为 MIT 授权的官方 x64 二进制，随安装包分发到程序目录（与 vpnd.exe 同层）。
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

namespace CrMcVpn
{
    /// <summary>Wintun 适配器 + 会话。线程安全性：Wintun 收发包 API 本身线程安全。</summary>
    internal sealed class WintunAdapter : IDisposable
    {
        public const uint MinRingCapacity = 0x20000;    // 128 KiB
        public const uint MaxRingCapacity = 0x4000000;  // 64 MiB

        private IntPtr _adapter;
        private IntPtr _session;
        private IntPtr _readEvent;

        public string Name { get; private set; }

        private WintunAdapter(string name, IntPtr adapter)
        {
            Name = name;
            _adapter = adapter;
        }

        /// <summary>打开已存在的适配器；不存在则创建。失败抛 IOException（驱动缺失等）。</summary>
        public static WintunAdapter OpenOrCreate(string name, string tunnelType, uint capacity)
        {
            string dll = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "wintun.dll");
            if (!File.Exists(dll))
            {
                throw new IOException("缺少 wintun.dll：" + dll);
            }
            IntPtr adapter = WintunOpenAdapter(name);
            if (adapter == IntPtr.Zero)
            {
                adapter = WintunCreateAdapter(name, tunnelType, IntPtr.Zero);
            }
            if (adapter == IntPtr.Zero)
            {
                throw new IOException("创建虚拟网卡失败（需要管理员权限/驱动签名），Win32="
                    + Marshal.GetLastWin32Error());
            }
            WintunAdapter wrapper = new WintunAdapter(name, adapter);
            wrapper.StartSession(capacity);
            return wrapper;
        }

        private void StartSession(uint capacity)
        {
            if (capacity < MinRingCapacity || capacity > MaxRingCapacity
                || (capacity & (capacity - 1)) != 0)
            {
                capacity = 0x400000; // 4 MiB
            }
            _session = WintunStartSession(_adapter, capacity);
            if (_session == IntPtr.Zero)
            {
                int error = Marshal.GetLastWin32Error();
                WintunCloseAdapter(_adapter);
                _adapter = IntPtr.Zero;
                throw new IOException("启动 Wintun 会话失败，Win32=" + error);
            }
            _readEvent = WintunGetReadWaitEvent(_session);
        }

        /// <summary>阻塞等待并取出一包（内部已复制，返回后缓冲可回收）。</summary>
        public byte[] Receive(int timeoutMs)
        {
            int waited = 0;
            while (true)
            {
                uint size = 0;
                IntPtr packet = WintunReceivePacket(_session, out size);
                if (packet != IntPtr.Zero)
                {
                    byte[] copy = new byte[size];
                    Marshal.Copy(packet, copy, 0, (int)size);
                    WintunReleaseReceivePacket(_session, packet);
                    return copy;
                }
                int error = Marshal.GetLastWin32Error();
                if (error == 259) // ERROR_NO_MORE_ITEMS：等待读事件
                {
                    if (_readEvent != IntPtr.Zero)
                    {
                        WintunWaitForSingleObject.Call(_readEvent,
                            (uint)(timeoutMs <= 0 ? 1000 : timeoutMs));
                    }
                    else
                    {
                        Thread.Sleep(5);
                    }
                    waited += timeoutMs;
                    if (timeoutMs > 0 && waited > timeoutMs)
                    {
                        return null;
                    }
                    continue;
                }
                if (error == 232 || error == 6) // 管道结束 / 句柄无效：适配器正在终止
                {
                    return null;
                }
                Thread.Sleep(5);
                waited += 5;
                if (timeoutMs > 0 && waited > timeoutMs)
                {
                    return null;
                }
            }
        }

        /// <summary>写入一包；环形缓冲满时短暂退避重试，最终丢弃（UDP 语义不保证）。</summary>
        public bool Send(byte[] packet)
        {
            for (int attempt = 0; attempt < 3; attempt++)
            {
                IntPtr buffer = WintunAllocateSendPacket(_session, (uint)packet.Length);
                if (buffer != IntPtr.Zero)
                {
                    Marshal.Copy(packet, 0, buffer, packet.Length);
                    WintunSendPacket(_session, buffer);
                    return true;
                }
                int error = Marshal.GetLastWin32Error();
                if (error == 232 || error == 6)
                {
                    return false;
                }
                Thread.Sleep(1);
            }
            return false;
        }

        public void Dispose()
        {
            if (_session != IntPtr.Zero)
            {
                WintunEndSession(_session);
                _session = IntPtr.Zero;
            }
            _readEvent = IntPtr.Zero;
            if (_adapter != IntPtr.Zero)
            {
                WintunCloseAdapter(_adapter);
                _adapter = IntPtr.Zero;
            }
        }

        /// <summary>用 netsh 给适配器配静态 IPv4（关闭 DHCP）。失败返回错误文本。</summary>
        public static string ConfigureIpv4(string adapterName, string vip)
        {
            string address = vip;
            string mask = "255.255.255.0";
            return NetShell.Run(new string[]
            {
                "interface", "ipv4", "set", "address",
                "name=" + adapterName,
                "source=static",
                "address=" + address,
                "mask=" + mask,
                "gateway=none",
            });
        }

        public static uint RunningDriverVersion()
        {
            return WintunGetRunningDriverVersion();
        }

        [DllImport("wintun.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr WintunCreateAdapter(string name, string tunnelType, IntPtr requestedGuid);

        [DllImport("wintun.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr WintunOpenAdapter(string name);

        [DllImport("wintun.dll", SetLastError = true)]
        private static extern void WintunCloseAdapter(IntPtr adapter);

        [DllImport("wintun.dll", SetLastError = true)]
        private static extern IntPtr WintunStartSession(IntPtr adapter, uint capacity);

        [DllImport("wintun.dll")]
        private static extern void WintunEndSession(IntPtr session);

        [DllImport("wintun.dll")]
        private static extern IntPtr WintunGetReadWaitEvent(IntPtr session);

        [DllImport("wintun.dll", SetLastError = true)]
        private static extern IntPtr WintunReceivePacket(IntPtr session, out uint packetSize);

        [DllImport("wintun.dll")]
        private static extern void WintunReleaseReceivePacket(IntPtr session, IntPtr packet);

        [DllImport("wintun.dll", SetLastError = true)]
        private static extern IntPtr WintunAllocateSendPacket(IntPtr session, uint packetSize);

        [DllImport("wintun.dll")]
        private static extern void WintunSendPacket(IntPtr session, IntPtr packet);

        [DllImport("wintun.dll")]
        private static extern uint WintunGetRunningDriverVersion();
    }

    /// <summary>kernel32!WaitForSingleObject 的最小封装（Wintun 只给我们事件句柄）。</summary>
    internal static class WintunWaitForSingleObject
    {
        [DllImport("kernel32.dll")]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

        public static void Call(IntPtr handle, uint milliseconds)
        {
            WaitForSingleObject(handle, milliseconds);
        }
    }
}
