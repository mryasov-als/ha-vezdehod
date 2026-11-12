using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Threading;

namespace VezdehodKeyHelper
{
    internal sealed class PrivateAdb : IAdbRunner, IDisposable
    {
        private const int TcpTableOwnerPidListener = 3;
        private const int InsufficientBuffer = 122;
        private static readonly int LoopbackAddress = BitConverter.ToInt32(IPAddress.Loopback.GetAddressBytes(), 0);

        private readonly Process server;
        private readonly int port;
        private readonly ProcessRunner client;
        private readonly string clientSocketOption;

        [StructLayout(LayoutKind.Sequential)]
        private struct TcpRow
        {
            public int State, LocalAddress, LocalPort, RemoteAddress, RemotePort, ProcessId;
        }

        [DllImport("iphlpapi.dll")]
        private static extern uint GetExtendedTcpTable(IntPtr table, ref int size, bool sorted,
            AddressFamily family, int tableClass, uint reserved);

        private PrivateAdb(Process server, int port, string executable)
        {
            this.server = server;
            this.port = port;
            client = new ProcessRunner(executable, ConfigureEnvironment);
            // A numeric host makes the ADB client treat this endpoint as non-autostartable.
            clientSocketOption = "-L tcp:127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture) + " ";
        }

        private static void ConfigureEnvironment(ProcessStartInfo info)
        {
            info.EnvironmentVariables.Remove("ADB_SERVER_SOCKET");
            info.EnvironmentVariables.Remove("ANDROID_ADB_SERVER_ADDRESS");
            info.EnvironmentVariables.Remove("ANDROID_ADB_SERVER_PORT");
            info.EnvironmentVariables.Remove("ADB_USB");
            info.EnvironmentVariables["ADB_MDNS"] = "0";
            info.EnvironmentVariables["ADB_EMU"] = "0";
        }

        private static int ChoosePort()
        {
            TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
            try
            {
                listener.ExclusiveAddressUse = true;
                listener.Start();
                return ((IPEndPoint)listener.LocalEndpoint).Port;
            }
            finally { listener.Stop(); }
        }

        private static bool OwnsListener(int port, int processId)
        {
            int size = 0;
            uint result = GetExtendedTcpTable(IntPtr.Zero, ref size, false,
                AddressFamily.InterNetwork, TcpTableOwnerPidListener, 0);
            while (result == InsufficientBuffer)
            {
                IntPtr table = Marshal.AllocHGlobal(size);
                try
                {
                    result = GetExtendedTcpTable(table, ref size, false,
                        AddressFamily.InterNetwork, TcpTableOwnerPidListener, 0);
                    if (result != 0) continue;
                    int count = Marshal.ReadInt32(table);
                    int rowSize = Marshal.SizeOf(typeof(TcpRow));
                    IntPtr rowPointer = IntPtr.Add(table, sizeof(int));
                    for (int i = 0; i < count; i++, rowPointer = IntPtr.Add(rowPointer, rowSize))
                    {
                        TcpRow row = (TcpRow)Marshal.PtrToStructure(rowPointer, typeof(TcpRow));
                        int localPort = (ushort)IPAddress.NetworkToHostOrder(unchecked((short)row.LocalPort));
                        if (row.ProcessId == processId && row.LocalAddress == LoopbackAddress && localPort == port)
                            return true;
                    }
                    return false;
                }
                finally { Marshal.FreeHGlobal(table); }
            }
            if (result != 0) throw new Win32Exception((int)result);
            return false;
        }

        private static ReaderException ServerError()
        {
            return new ReaderException("adb_server", "Не удалось подключиться к встроенному ADB. Запустите поиск снова.");
        }

        public static PrivateAdb Start(string executable, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            Process process = new Process();
            bool ready = false;
            try
            {
                int port = ChoosePort();
                process.StartInfo = ProcessRunner.CreateStartInfo(executable,
                    "-L tcp:localhost:" + port.ToString(CultureInfo.InvariantCulture) + " server nodaemon");
                ConfigureEnvironment(process.StartInfo);
                process.OutputDataReceived += delegate { };
                process.ErrorDataReceived += delegate { };
                token.ThrowIfCancellationRequested();
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                Stopwatch elapsed = Stopwatch.StartNew();
                while (!process.HasExited && elapsed.ElapsedMilliseconds < 8000)
                {
                    token.ThrowIfCancellationRequested();
                    if (OwnsListener(port, process.Id))
                    {
                        PrivateAdb session = new PrivateAdb(process, port, executable);
                        ready = true;
                        return session;
                    }
                    token.WaitHandle.WaitOne(25);
                }
                token.ThrowIfCancellationRequested();
                throw ServerError();
            }
            catch (OperationCanceledException) { throw; }
            catch (ReaderException) { throw; }
            catch (Exception) { throw ServerError(); }
            finally { if (!ready) StopServer(process); }
        }

        public CommandResult Run(string arguments, CancellationToken token, int timeoutMs)
        {
            token.ThrowIfCancellationRequested();
            try
            {
                if (server.HasExited || !OwnsListener(port, server.Id)) throw ServerError();
            }
            catch (Win32Exception) { throw ServerError(); }
            return client.Run(clientSocketOption + arguments, token, timeoutMs);
        }

        private static void StopServer(Process process)
        {
            using (process)
            {
                ProcessRunner.Stop(process);
                try { process.WaitForExit(1000); } catch (InvalidOperationException) { }
            }
        }

        public void Dispose() { StopServer(server); }
    }
}
