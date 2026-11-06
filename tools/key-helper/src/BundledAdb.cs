using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;

namespace VezdehodKeyHelper
{
    public static class BundledAdb
    {
        private static readonly object Gate = new object();
        public static string CacheDirectory
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "VezdehodKeyHelper", "adb-" + BundleInfo.ArchiveHash.Substring(0, 12)); }
        }

        private static string Hash(byte[] data)
        {
            using (SHA256 sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(data)).Replace("-", "").ToLowerInvariant();
        }

        private static bool Valid(string path, string expected)
        {
            return File.Exists(path) && Hash(File.ReadAllBytes(path)) == expected;
        }

        public static string Ensure(CancellationToken token)
        {
            lock (Gate)
            {
                token.ThrowIfCancellationRequested();
                try
                {
                    string root = CacheDirectory;
                    Directory.CreateDirectory(root);
                    using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("Vezdehod.Adb.zip"))
                    using (ZipArchive archive = new ZipArchive(resource, ZipArchiveMode.Read))
                    {
                        if (archive.Entries.Count != BundleInfo.Files.Count)
                            throw new IOException("Invalid resource count");
                        foreach (ZipArchiveEntry entry in archive.Entries)
                        {
                            token.ThrowIfCancellationRequested();
                            string expected;
                            if (!BundleInfo.Files.TryGetValue(entry.FullName, out expected) ||
                                Path.GetFileName(entry.FullName) != entry.FullName || entry.Length > 16 * 1024 * 1024)
                                throw new IOException("Invalid resource entry");
                            string target = Path.Combine(root, entry.FullName);
                            if (Valid(target, expected)) continue;
                            byte[] bytes;
                            using (MemoryStream buffer = new MemoryStream())
                            using (Stream input = entry.Open()) { input.CopyTo(buffer); bytes = buffer.ToArray(); }
                            if (Hash(bytes) != expected) throw new IOException("Invalid resource hash");
                            string temporary = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
                            try
                            {
                                File.WriteAllBytes(temporary, bytes);
                                if (File.Exists(target)) File.Replace(temporary, target, null);
                                else File.Move(temporary, target);
                            }
                            finally { if (File.Exists(temporary)) File.Delete(temporary); }
                        }
                    }
                    return Path.Combine(root, "adb.exe");
                }
                catch (OperationCanceledException) { throw; }
                catch (Exception)
                { throw new ReaderException("bundle", "Не удалось подготовить встроенный ADB. Проверьте доступ к папке пользователя и запустите программу снова."); }
            }
        }

        public static void OpenNotice()
        {
            Ensure(CancellationToken.None);
            System.Diagnostics.Process.Start(Path.Combine(Environment.SystemDirectory, "notepad.exe"),
                "\"" + Path.Combine(CacheDirectory, "NOTICE.txt") + "\"");
        }
    }
}
