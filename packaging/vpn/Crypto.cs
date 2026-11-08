// 数据面密码学：HKDF-SHA256 派生、AES-256-GCM（Windows CNG）、64 包抗重放窗口。
//
// 只依赖 .NET Framework 4.x 自带的 System.Security.Cryptography 与 CNG（bcrypt.dll），
// 不引入第三方库，保证 csc 单命令可编译。
using System;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace CrMcVpn
{
    internal static class Hkdf
    {
        public const int KeyBytes = 32;

        public static byte[] Sha256(byte[] data)
        {
            using (SHA256 sha = SHA256.Create())
            {
                return sha.ComputeHash(data);
            }
        }

        /// <summary>HKDF-SHA256：extract + expand（RFC 5869）。salt 为空时用 32 字节零。</summary>
        public static byte[] Derive(byte[] ikm, byte[] salt, string info, int length)
        {
            if (ikm == null || ikm.Length == 0)
            {
                throw new ArgumentException("HKDF 输入密钥不能为空");
            }
            byte[] useSalt = salt;
            if (useSalt == null || useSalt.Length == 0)
            {
                useSalt = new byte[32];
            }
            byte[] infoBytes = Encoding.UTF8.GetBytes(info ?? "");
            using (HMACSHA256 extract = new HMACSHA256(useSalt))
            {
                byte[] prk = extract.ComputeHash(ikm);
                using (HMACSHA256 expand = new HMACSHA256(prk))
                {
                    int blocks = (length + 31) / 32;
                    byte[] okm = new byte[length];
                    byte[] previous = new byte[0];
                    for (int i = 0; i < blocks; i++)
                    {
                        byte[] message = new byte[previous.Length + infoBytes.Length + 1];
                        Buffer.BlockCopy(previous, 0, message, 0, previous.Length);
                        Buffer.BlockCopy(infoBytes, 0, message, previous.Length, infoBytes.Length);
                        message[message.Length - 1] = (byte)(i + 1);
                        previous = expand.ComputeHash(message);
                        int offset = i * 32;
                        int count = Math.Min(32, length - offset);
                        Buffer.BlockCopy(previous, 0, okm, offset, count);
                    }
                    return okm;
                }
            }
        }
    }

    /// <summary>一条单向通道的序号计数器 + 接收侧 64 包滑动窗口。</summary>
    internal sealed class ReplayWindow
    {
        private bool _seenAny;
        private ulong _highest;
        private ulong _bitmap;

        /// <summary>序号可接受则返回真并推进窗口；重复或过旧返回假。</summary>
        public bool Accept(uint seq)
        {
            ulong value = seq;
            if (!_seenAny)
            {
                _seenAny = true;
                _highest = value;
                _bitmap = 1UL;
                return true;
            }
            if (value > _highest)
            {
                ulong shift = value - _highest;
                _bitmap = shift >= 64 ? 1UL : (_bitmap << (int)shift) | 1UL;
                _highest = value;
                return true;
            }
            ulong delta = _highest - value;
            if (delta >= 64)
            {
                return false;
            }
            ulong mask = 1UL << (int)delta;
            if ((_bitmap & mask) != 0)
            {
                return false;
            }
            _bitmap |= mask;
            return true;
        }
    }

    /// <summary>BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO（x64 下 88 字节，顺序布局自动补齐）。</summary>
    [StructLayout(LayoutKind.Sequential)]
    internal struct BcryptAuthModeInfo
    {
        public int cbSize;
        public int dwInfoVersion;
        public IntPtr pbNonce;
        public int cbNonce;
        public IntPtr pbAuthData;
        public int cbAuthData;
        public IntPtr pbTag;
        public int cbTag;
        public IntPtr pbMacContext;
        public int cbMacContext;
        public int cbAAD;
        public long cbData;
        public int dwFlags;
    }

    /// <summary>AES-256-GCM 密钥句柄：一次派生、反复加解密。</summary>
    internal sealed class AesGcmKey : IDisposable
    {
        public const int NonceBytes = 12;
        public const int TagBytes = 16;

        private static IntPtr _algorithm;
        private static readonly object _algLock = new object();

        private IntPtr _key;

        public AesGcmKey(byte[] key)
        {
            if (key == null || key.Length != 32)
            {
                throw new ArgumentException("AES-256-GCM 密钥必须是 32 字节");
            }
            int status = BCryptGenerateSymmetricKey(Algorithm(), out _key, IntPtr.Zero, 0, key, key.Length, 0);
            Check(status, "BCryptGenerateSymmetricKey");
        }

        private static IntPtr Algorithm()
        {
            lock (_algLock)
            {
                if (_algorithm == IntPtr.Zero)
                {
                    IntPtr alg;
                    int status = BCryptOpenAlgorithmProvider(out alg, "AES", null, 0);
                    Check(status, "BCryptOpenAlgorithmProvider");
                    // GCM 链模式：值必须带结尾 0、UTF-16 编码
                    byte[] mode = Encoding.Unicode.GetBytes("ChainingModeGCM\0");
                    status = BCryptSetProperty(alg, "ChainingMode", mode, mode.Length, 0);
                    Check(status, "BCryptSetProperty(GCM)");
                    _algorithm = alg;
                }
                return _algorithm;
            }
        }

        /// <summary>加密：返回密文，认证标签写入 <paramref name="tag"/>（16 字节）。</summary>
        public byte[] Seal(byte[] nonce, byte[] aad, byte[] plaintext, out byte[] tag)
        {
            tag = new byte[TagBytes];
            byte[] cipher = new byte[plaintext.Length];
            GCHandle noncePin = GCHandle.Alloc(nonce, GCHandleType.Pinned);
            GCHandle aadPin = GCHandle.Alloc(aad, GCHandleType.Pinned);
            GCHandle tagPin = GCHandle.Alloc(tag, GCHandleType.Pinned);
            try
            {
                BcryptAuthModeInfo info = NewInfo();
                info.pbNonce = noncePin.AddrOfPinnedObject();
                info.cbNonce = nonce.Length;
                info.pbAuthData = aadPin.AddrOfPinnedObject();
                info.cbAuthData = aad.Length;
                info.pbTag = tagPin.AddrOfPinnedObject();
                info.cbTag = TagBytes;
                IntPtr infoPtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(BcryptAuthModeInfo)));
                try
                {
                    Marshal.StructureToPtr(info, infoPtr, false);
                    int result;
                    int status = BCryptEncrypt(_key, plaintext, plaintext.Length, infoPtr,
                        null, 0, cipher, cipher.Length, out result, 0);
                    Check(status, "BCryptEncrypt");
                }
                finally
                {
                    Marshal.FreeHGlobal(infoPtr);
                }
                return cipher;
            }
            finally
            {
                tagPin.Free();
                aadPin.Free();
                noncePin.Free();
            }
        }

        /// <summary>解密并校验标签；密钥不符 / 篡改抛 CryptographicException。</summary>
        public byte[] Open(byte[] nonce, byte[] aad, byte[] ciphertext, byte[] tag)
        {
            if (tag == null || tag.Length != TagBytes)
            {
                throw new CryptographicException("认证标签长度非法");
            }
            byte[] plain = new byte[ciphertext.Length];
            GCHandle noncePin = GCHandle.Alloc(nonce, GCHandleType.Pinned);
            GCHandle aadPin = GCHandle.Alloc(aad, GCHandleType.Pinned);
            GCHandle tagPin = GCHandle.Alloc(tag, GCHandleType.Pinned);
            try
            {
                BcryptAuthModeInfo info = NewInfo();
                info.pbNonce = noncePin.AddrOfPinnedObject();
                info.cbNonce = nonce.Length;
                info.pbAuthData = aadPin.AddrOfPinnedObject();
                info.cbAuthData = aad.Length;
                info.pbTag = tagPin.AddrOfPinnedObject();
                info.cbTag = TagBytes;
                IntPtr infoPtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(BcryptAuthModeInfo)));
                try
                {
                    Marshal.StructureToPtr(info, infoPtr, false);
                    int result;
                    int status = BCryptDecrypt(_key, ciphertext, ciphertext.Length, infoPtr,
                        null, 0, plain, plain.Length, out result, 0);
                    if (status != 0)
                    {
                        throw new CryptographicException("AEAD 校验失败（密钥不符或被篡改）");
                    }
                }
                finally
                {
                    Marshal.FreeHGlobal(infoPtr);
                }
                return plain;
            }
            finally
            {
                tagPin.Free();
                aadPin.Free();
                noncePin.Free();
            }
        }

        private static BcryptAuthModeInfo NewInfo()
        {
            BcryptAuthModeInfo info = new BcryptAuthModeInfo();
            info.cbSize = Marshal.SizeOf(typeof(BcryptAuthModeInfo));
            info.dwInfoVersion = 1;
            return info;
        }

        public void Dispose()
        {
            if (_key != IntPtr.Zero)
            {
                BCryptDestroyKey(_key);
                _key = IntPtr.Zero;
            }
        }

        private static void Check(int status, string what)
        {
            if (status != 0)
            {
                throw new CryptographicException(what + " 失败，NTSTATUS=0x" + status.ToString("X8"));
            }
        }

        [DllImport("bcrypt.dll", CharSet = CharSet.Unicode)]
        private static extern int BCryptOpenAlgorithmProvider(out IntPtr phAlgorithm, string pszAlgId,
            string pszImplementation, uint dwFlags);

        [DllImport("bcrypt.dll", CharSet = CharSet.Unicode)]
        private static extern int BCryptSetProperty(IntPtr hObject, string pszProperty,
            byte[] pbInput, int cbInput, int dwFlags);

        [DllImport("bcrypt.dll")]
        private static extern int BCryptGenerateSymmetricKey(IntPtr hAlgorithm, out IntPtr phKey,
            IntPtr pbKeyObject, int cbKeyObject, byte[] pbSecret, int cbSecret, int dwFlags);

        [DllImport("bcrypt.dll")]
        private static extern int BCryptEncrypt(IntPtr hKey, byte[] pbInput, int cbInput,
            IntPtr pPaddingInfo, byte[] pbIV, int cbIV, byte[] pbOutput, int cbOutput,
            out int pcbResult, int dwFlags);

        [DllImport("bcrypt.dll")]
        private static extern int BCryptDecrypt(IntPtr hKey, byte[] pbInput, int cbInput,
            IntPtr pPaddingInfo, byte[] pbIV, int cbIV, byte[] pbOutput, int cbOutput,
            out int pcbResult, int dwFlags);

        [DllImport("bcrypt.dll")]
        private static extern int BCryptDestroyKey(IntPtr hKey);
    }
}
