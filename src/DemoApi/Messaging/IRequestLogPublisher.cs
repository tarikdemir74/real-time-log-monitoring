using DemoApi.Logging;

namespace DemoApi.Messaging;

public interface IRequestLogPublisher
{
    Task PublishAsync(RequestLogEntry entry);
}
