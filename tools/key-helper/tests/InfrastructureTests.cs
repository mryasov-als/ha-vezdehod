using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
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
            ProcessRunner runner = new ProcessRunner(Path.GetFullPath(args[0]));
            CommandResult result = runner.Run("normal", CancellationToken.None, 3000);
            Check(result.ExitCode == 23 && result.Output.Contains("fixture-output") && result.Error.Contains("fixture-error"),
                "reads both pipes and exit status");
            string marker = Path.GetFullPath("build/cancel-pid.txt");
            if (File.Exists(marker)) File.Delete(marker);
            CancellationTokenSource cancel = new CancellationTokenSource();
            cancel.CancelAfter(300);
            Stopwatch clock = Stopwatch.StartNew();
            bool cancelled = false;
            try { runner.Run("slow " + marker, cancel.Token, 10000); }
            catch (OperationCanceledException) { cancelled = true; }
            Check(cancelled && clock.Elapsed.TotalSeconds < 3, "prompt cancellation");
            if (File.Exists(marker))
            {
                int pid = Int32.Parse(File.ReadAllText(marker));
                bool stopped;
                try { using (Process child = Process.GetProcessById(pid)) stopped = child.HasExited; }
                catch (ArgumentException) { stopped = true; }
                Check(stopped, "owned child stops on cancel");
            }
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
            Console.WriteLine("PASS: " + passed + " process checks; only test children launched");
            return 0;
        }
        catch (Exception ex) { Console.Error.WriteLine(ex.Message); return 1; }
    }
}
