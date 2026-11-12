using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
using VezdehodKeyHelper;

internal static class InfrastructureTests
{
    private static int passed;
    private static void Check(bool condition, string name)
    { if (!condition) throw new Exception("FAIL: " + name); passed++; }

    public static int Main(string[] args)
    {
        try
        {
            string executable = Path.GetFullPath(args[0]);
            ProcessRunner runner = new ProcessRunner(executable);
            CommandResult result = runner.Run("normal", CancellationToken.None, 3000);
            Check(result.ExitCode == 23 && result.Output.Contains("fixture-output") && result.Error.Contains("fixture-error"),
                "reads both pipes and exit status");
            CheckCancellation(runner);
            bool timedOut = false;
            try { runner.Run("slow", CancellationToken.None, 150); }
            catch (ReaderException ex) { timedOut = ex.Code == "adb_timeout"; }
            Check(timedOut, "command timeout");
            bool bounded = false;
            try { runner.Run("flood", CancellationToken.None, 5000); }
            catch (ReaderException ex) { bounded = ex.Code == "output_limit"; }
            Check(bounded, "output memory bound");
            bool safe = false;
            try { new ProcessRunner("Z:/nonexistent/fixture.exe").Run("", CancellationToken.None, 500); }
            catch (ReaderException ex) { safe = ex.Code == "adb_start" && !ex.Message.Contains("nonexistent"); }
            Check(safe, "safe startup error");
            CheckPrivateServer(executable);
            Console.WriteLine("PASS: " + passed + " process checks; only test children launched");
            return 0;
        }
        catch (Exception ex) { Console.Error.WriteLine(ex.Message); return 1; }
    }

    private static void CheckCancellation(ProcessRunner runner)
    {
        string marker = Path.Combine(Path.GetTempPath(), "Vezdehod child " + Guid.NewGuid().ToString("N") + ".txt");
        using (CancellationTokenSource cancel = new CancellationTokenSource())
        {
            Task<bool> command = Task.Run(delegate {
                try { runner.Run("slow \"" + marker + "\"", cancel.Token, 10000); }
                catch (OperationCanceledException) { return true; }
                return false;
            });
            try
            {
                SpinWait.SpinUntil(() => File.Exists(marker) || command.IsCompleted, 5000);
                Check(File.Exists(marker), "child wrote PID to a path with spaces before cancellation");
                using (Process child = Process.GetProcessById(Int32.Parse(File.ReadAllText(marker))))
                {
                    try
                    {
                        Check(!child.HasExited, "child is running before cancellation");
                        Stopwatch clock = Stopwatch.StartNew();
                        cancel.Cancel();
                        Check(command.Wait(3000) && command.Result && clock.ElapsedMilliseconds < 3000,
                            "prompt cancellation");
                        Check(child.WaitForExit(1000), "owned child stops on cancel");
                    }
                    finally { if (!child.HasExited) child.Kill(); }
                }
            }
            finally
            {
                cancel.Cancel();
                SpinWait.SpinUntil(() => command.IsCompleted, 1000);
                File.Delete(marker);
                File.Delete(marker + ".tmp");
            }
        }
    }

    private static void CheckPrivateServer(string executable)
    {
        TcpListener otherServer = new TcpListener(IPAddress.Loopback, 0);
        otherServer.ExclusiveAddressUse = true;
        otherServer.Start();
        string[] names = { "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT", "ADB_USB" };
        string[] previous = Array.ConvertAll(names, Environment.GetEnvironmentVariable);
        string port = ((IPEndPoint)otherServer.LocalEndpoint).Port.ToString();
        try
        {
            Environment.SetEnvironmentVariable("ADB_SERVER_SOCKET", "tcp:127.0.0.1:" + port);
            Environment.SetEnvironmentVariable("ANDROID_ADB_SERVER_ADDRESS", "127.0.0.1");
            Environment.SetEnvironmentVariable("ANDROID_ADB_SERVER_PORT", port);
            Environment.SetEnvironmentVariable("ADB_USB", "0");

            Process owned;
            using (PrivateAdb adb = PrivateAdb.Start(executable, CancellationToken.None))
            {
                CommandResult result = adb.Run("fixture", CancellationToken.None, 3000);
                int pid = 0;
                Check(result.ExitCode == 0 && Int32.TryParse(result.Output.Trim(), out pid),
                    "client reaches private server despite inherited ADB environment");
                owned = Process.GetProcessById(pid);
                Check(!owned.HasExited, "private server is running during its session");
            }
            using (owned)
                Check(owned.WaitForExit(1000), "private server stops when its session ends");

            using (PrivateAdb adb = PrivateAdb.Start(executable, CancellationToken.None))
            {
                CommandResult result = adb.Run("fixture", CancellationToken.None, 3000);
                using (Process child = Process.GetProcessById(Int32.Parse(result.Output.Trim())))
                {
                    child.Kill();
                    if (!child.WaitForExit(1000)) throw new Exception("Fixture server failed to exit");
                }
                bool refused = false;
                try { adb.Run("fixture", CancellationToken.None, 1000); }
                catch (ReaderException ex) { refused = ex.Code == "adb_server"; }
                Check(refused, "lost private server fails without falling back");
            }
            Check(!otherServer.Pending() && Environment.GetEnvironmentVariable("ANDROID_ADB_SERVER_PORT") == port,
                "unrelated listener and parent environment untouched");
        }
        finally
        {
            for (int i = 0; i < names.Length; i++) Environment.SetEnvironmentVariable(names[i], previous[i]);
            otherServer.Stop();
        }
    }
}
