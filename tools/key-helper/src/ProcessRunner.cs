using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

namespace VezdehodKeyHelper
{
    public sealed class ProcessRunner : IAdbRunner
    {
        private readonly string executable;
        private const int OutputLimit = 2 * 1024 * 1024;
        public ProcessRunner(string path) { executable = path; }

        private static void Stop(Process process)
        {
            try { if (!process.HasExited) process.Kill(); }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
        }

        public CommandResult Run(string arguments, CancellationToken token, int timeoutMs)
        {
            token.ThrowIfCancellationRequested();
            using (Process process = new Process())
            using (ManualResetEvent outputEnded = new ManualResetEvent(false))
            using (ManualResetEvent errorEnded = new ManualResetEvent(false))
            {
                object gate = new object();
                StringBuilder output = new StringBuilder(), error = new StringBuilder();
                bool overflow = false;
                process.StartInfo = new ProcessStartInfo {
                    FileName = executable, Arguments = arguments,
                    WorkingDirectory = Path.GetDirectoryName(executable),
                    UseShellExecute = false, CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                    RedirectStandardOutput = true, RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
                };
                DataReceivedEventHandler readOutput = delegate(object sender, DataReceivedEventArgs e) {
                    lock (gate)
                    {
                        if (e.Data == null) { try { outputEnded.Set(); } catch (ObjectDisposedException) { } }
                        else if (output.Length + error.Length + e.Data.Length < OutputLimit) output.AppendLine(e.Data);
                        else overflow = true;
                    }
                };
                DataReceivedEventHandler readError = delegate(object sender, DataReceivedEventArgs e) {
                    lock (gate)
                    {
                        if (e.Data == null) { try { errorEnded.Set(); } catch (ObjectDisposedException) { } }
                        else if (output.Length + error.Length + e.Data.Length < OutputLimit) error.AppendLine(e.Data);
                        else overflow = true;
                    }
                };
                try
                {
                    process.OutputDataReceived += readOutput;
                    process.ErrorDataReceived += readError;
                    process.Start();
                    using (token.Register(delegate { Stop(process); }))
                    {
                        process.BeginOutputReadLine();
                        process.BeginErrorReadLine();
                        Stopwatch clock = Stopwatch.StartNew();
                        while (!process.WaitForExit(40))
                        {
                            token.ThrowIfCancellationRequested();
                            if (clock.ElapsedMilliseconds >= timeoutMs)
                                throw new ReaderException("adb_timeout", "Телефон слишком долго не отвечает. Проверьте подключение и повторите поиск.");
                            lock (gate) if (overflow)
                                throw new ReaderException("output_limit", "Слишком большой ответ журнала. Нажмите «Старт» и откройте список шлагбаумов в «Вездеходе».");
                        }
                        token.ThrowIfCancellationRequested();
                        outputEnded.WaitOne(800);
                        errorEnded.WaitOne(800);
                        token.ThrowIfCancellationRequested();
                        lock (gate)
                        {
                            if (overflow) throw new ReaderException("output_limit", "Слишком большой ответ журнала. Повторите поиск.");
                            return new CommandResult(process.ExitCode, output.ToString(), error.ToString());
                        }
                    }
                }
                catch (OperationCanceledException) { throw; }
                catch (ReaderException) { throw; }
                catch (Exception)
                { throw new ReaderException("adb_start", "Не удалось запустить встроенный ADB. Перезапустите программу или распакуйте её заново."); }
                finally
                {
                    Stop(process);
                    try { process.WaitForExit(500); } catch (InvalidOperationException) { }
                    try { process.CancelOutputRead(); } catch (InvalidOperationException) { }
                    try { process.CancelErrorRead(); } catch (InvalidOperationException) { }
                    process.OutputDataReceived -= readOutput;
                    process.ErrorDataReceived -= readError;
                }
            }
        }
    }
}
