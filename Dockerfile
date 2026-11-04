# ConFL image: the fact extractor (built here) plus the Python package; the LLM is reached over the network.
#   docker build -t confl .
#   docker run --rm -e OPENAI_API_KEY -v "$PWD":/work confl localize --source my-project --report bug.txt
FROM maven:3.9.9-eclipse-temurin-17 AS extractor
WORKDIR /build
COPY FactsExtractor/pom.xml .
RUN mvn -q -B dependency:go-offline
COPY FactsExtractor/src src
RUN mvn -q -B package -DskipTests

FROM eclipse-temurin:17-jre
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/confl
COPY confl confl
COPY tools tools
COPY configs configs
COPY ground-truth ground-truth
COPY examples examples
COPY --from=extractor /build/target/ConFL-1.0-SNAPSHOT.jar FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar
# Readable by any --user, whatever the permissions of the build context.
RUN chmod -R a+rX /opt/confl
ENV PYTHONPATH=/opt/confl PYTHONDONTWRITEBYTECODE=1
# Paths given on the command line are relative to the mounted working directory.
WORKDIR /work
ENTRYPOINT ["python3", "-m", "confl"]
CMD ["--help"]
